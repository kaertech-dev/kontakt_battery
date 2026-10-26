from PyQt5.uic import loadUi
from PyQt5.QtWidgets import QDialog, QMessageBox
from PyQt5 import QtWidgets
from PyQt5.QtCore import QTimer, Qt
import configparser
import sys
import os
import csv
import datetime
import re
import serial
import pymysql
from pymysql.cursors import DictCursor

class BatteryTestApp(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        def resource_path(relative_path):
            try:
                base_path = sys._MEIPASS  # PyInstaller temp folder
            except Exception:
                base_path = os.path.abspath(".")
            return os.path.join(base_path, relative_path)
        loadUi(resource_path("battery_tester_design.ui"), self)

        # Load voltage limits, port config, and test mode from limits.txt
        self.min_voltage = 1.45
        self.max_voltage = 1.65
        self.port_config_path = {}
        self.test_mode = "Production"  # Default to Production mode
        self.previous_stations = []
        try:
            with open("limits.txt", "r") as f:
                for line in f:
                    line = line.strip()
                    if "=" in line:
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip()
                        if key == "MinV":
                            self.min_voltage = float(value)
                        elif key == "MaxV":
                            self.max_voltage = float(value)
                        elif key == "test mode":
                            self.test_mode = value
                        else:
                            self.port_config_path[key] = value

            self.description = self.port_config_path.get("PORT")
            print(f"Test Mode: {self.test_mode}")
            self.setWindowTitle(self.test_mode)

        except Exception as e:
            print(f"Error reading limits file: {e}")

        try:
            db_details = configparser.RawConfigParser()
            db_details.read('db_config.ini')
            self.db_host = db_details['Host'].get('Host')
            self.schema = db_details['Database'].get('Schema')
            self.model = db_details['Database'].get('Model')
            self.main_table = f"{self.model}_main"
            station_t = db_details['Database'].get('Station')
            self.station = f"{self.model}_{station_t}"

        except Exception as e:
            print(f"Error reading limits file: {e}")
            self.db_host = '192.168.20.210'
            self.schema = 'kontakt'
            self.model = 'nanoirtag'
            self.main_table = 'nanoirtag_main'
            self.station = 'nanoirtag_battery'


        self.dmm = None
        port_name = self.port_config_path.get("PORT")

        if port_name:
            try:
                self.dmm = serial.Serial(port_name, baudrate=9600, timeout=1)
                self.portreflector.setText(f"Connected: {port_name}")
                self.voltagereader.setEnabled(True)
            except Exception as e:
                self.portreflector.setText("Connection Failed")
                self.voltagereader.setText("Connection Failed")
                print(f"Serial connection error: {e}")
        else:
            self.portreflector.setText("No Port Configured")
            self.voltagereader.setText("No Port Configured")

        self.message_form = QDialog()
        loadUi(resource_path("messageform.ui"), self.message_form)

        self.loginbutton.clicked.connect(self.login)
        self.logoutbutton.clicked.connect(self.logout)
        self.message_form.pushbuttonerror.clicked.connect(self.quit)

        # Serial field validates on Enter; Start button begins the test
        self.serialnumber.returnPressed.connect(self.validate_serial)
        self.startbutton.clicked.connect(self.start_test)

        self.set_light(self.lightPassorFail, "lightgrey")

        self.serialnumber.setEnabled(False)
        self.progressBar.setEnabled(False)
        self.portreflector.setEnabled(False)
        self.startbutton.setEnabled(False)
        self.progressBar.setValue(0)
        self.update_ui()

        self.timer = QTimer()
        self.timer.timeout.connect(self.update_voltage_display)

        if self.dmm and self.dmm.is_open:
            self.timer.start(1000)

        self.test_timer = QTimer()
        self.test_timer.timeout.connect(self.update_test_progress)

        self.test_time_elapsed = 0
        self.test_duration = 1
        self.progressBar.setMinimum(0)
        self.progressBar.setMaximum(100)
        self.progressBar.setValue(0)

        # current_serial: the validated base serial ready for testing
        self.current_serial = None
        # serial_ready: True when a serial has been validated and is waiting for Start
        self.serial_ready = False

    def login(self):
        ke_no = self.operatoren.text().strip().upper()
        shift = self.shift.currentText().strip().upper()

        if ke_no == "":
            self.message_form.errorlabel.setText("Set Your KE no.!!")
            self.message_form.show()
            return
        elif shift == "" or shift == "--SELECT--":
            self.message_form.errorlabel.setText("Set Your Shift!!")
            self.message_form.show()
            return

        self.loginbutton.setEnabled(False)
        self.operatoren.setEnabled(False)
        self.shift.setEnabled(False)
        self.progressBar.setEnabled(True)
        self.serialnumber.setEnabled(True)
        self.startbutton.setEnabled(False)

    # ------------------------------------------------------------------
    # STEP 1 — Operator scans / types serial and presses Enter
    #          Runs all DB checks; if OK, arms the Start button
    # ------------------------------------------------------------------
    def validate_serial(self):
        if self.test_timer.isActive():
            QMessageBox.warning(self, "Wait", "Test in progress...")
            return

        serial_num = self.serialnumber.text().strip()
        valid_format = re.match(r'K\w{11}', serial_num, re.IGNORECASE)

        if not serial_num or not valid_format:
            QMessageBox.warning(self, "Missing Serial", "Enter a valid serial number.")
            self.serialnumber.clear()
            self._disarm_start()
            return

        # ── Check if already passed ────────────────────────────────────
        if self.has_passed_before(serial_num):
            QMessageBox.information(
                self,
                "Already Passed",
                "This serial has already passed and cannot be tested again."
            )
            self.serialnumber.clear()
            self._disarm_start()
            return

        # ── Check previous fail records ────────────────────────────────
        fail_count = self.count_previous_fails(serial_num)
        if fail_count > 0:
            reply = QMessageBox.question(
                self,
                "Previous Failure Detected",
                f"Serial <b>{serial_num}</b> has failed <b>{fail_count}</b> time(s) before.<br><br>"
                f"Do you want to continue and test this unit again?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply == QMessageBox.No:
                self.serialnumber.clear()
                self._disarm_start()
                return

        # ── Check station prerequisites ────────────────────────────────
        ok, message = self.check_station_status(serial_num)
        if not ok:
            QMessageBox.warning(self, "Station Check Failed", message)
            self.write_row(serial_num, f"Rejected - {message}")
            self.serialnumber.clear()
            self._disarm_start()
            return

        # ── All checks passed — arm the Start button ───────────────────
        self.current_serial = serial_num
        self.serial_ready = True
        self.startbutton.setEnabled(True)
        self.set_light(self.lightPassorFail, "lightgrey")
        self.progressBar.setValue(0)
        self.serialnumber.setReadOnly(False)

    # ------------------------------------------------------------------
    # STEP 2 — Operator clicks Start button to begin the timed test
    # ------------------------------------------------------------------
    def start_test(self):
        if self.test_timer.isActive():
            QMessageBox.warning(self, "Wait", "Test already in progress...")
            return

        if not self.serial_ready or not self.current_serial:
            QMessageBox.warning(self, "No Serial", "Scan a serial number first.")
            return

        valid_format = re.match(r'K\w{11}', self.current_serial, re.IGNORECASE)
        if not valid_format:
            QMessageBox.warning(self, "Missing Serial", "Enter a valid serial number.")
            self.current_serial = None
            self._disarm_start()
            return

        self.startbutton.setEnabled(False)
        self.serialnumber.setReadOnly(False)
        self.serialnumber.clear()

        self.test_time_elapsed = 0
        self.progressBar.setValue(0)
        self.progressBar.setStyleSheet("")
        self.test_timer.start(100)

    def _disarm_start(self):
        """Reset armed state so Start cannot fire without a fresh valid scan."""
        self.serial_ready = False
        self.current_serial = None
        self.startbutton.setEnabled(False)
        self.serialnumber.setReadOnly(False)

    # ------------------------------------------------------------------

    def update_test_progress(self):
        self.test_time_elapsed += 0.1

        progress_percent = int((self.test_time_elapsed / self.test_duration) * 100)
        self.progressBar.setValue(progress_percent)

        if self.test_time_elapsed >= self.test_duration:
            self.test_timer.stop()
            self.progressBar.setValue(100)
            self.run_test_after_delay()

    # ---------------------------------------------------------------------------
    # Helper: build the suffixed serial for a failed repeat
    #   1st fail  → base serial + "_1"
    #   2nd fail  → base_serial + "_2"
    #   3rd fail  → base_serial + "_3"
    # ---------------------------------------------------------------------------
    def get_fail_serial(self, base_serial):
        if self.test_mode == 'Development':
            return base_serial
        fail_count = self.count_previous_fails(base_serial)
        # if fail_count == 0:
        #     return base_serial
        # else:
        return f"{base_serial}_{fail_count+1}"

    def count_previous_fails(self, base_serial):
        if self.test_mode == "Development":
            # return self._count_previous_fails_csv(base_serial)
            return 0
        else:
            return self._count_previous_fails_db(base_serial)

    def _count_previous_fails_db(self, base_serial):
        try:
            conn = pymysql.connect(
                host=self.db_host,
                user="labeling",
                password="labeling",
                database=self.schema
            )
            cursor = conn.cursor()
            query = """
                    SELECT COUNT(*) FROM """ + self.station + """ 
                    WHERE (serial_num = %s OR serial_num LIKE %s)
                        AND status = 0
                    """
            cursor.execute(
                query,
                (base_serial, f"{base_serial}\\_%")
            )
            result = cursor.fetchone()
            conn.close()
            return result[0] if result else 0
        except Exception:
            return 0

    def _count_previous_fails_csv(self, base_serial):
        count = 0
        if not os.path.isfile("results.csv"):
            return 0
        try:
            with open("results.csv", "r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    serial_col = row.get("Serial", "")
                    status_col = str(row.get("Status", "")).strip()
                    if serial_col.startswith(f"{base_serial}_"):
                        if status_col == "0":
                            count += 1
        except Exception:
            pass
        return count

    # ---------------------------------------------------------------------------

    def run_test_after_delay(self):
        voltage = self.read_voltage()

        operator = self.operatoren.text().strip()
        shift = self.shift.currentText().strip()
        po_ok, po_num = self.check_po_num(self.current_serial)
        if not po_ok and self.test_mode == 'Production':
            QMessageBox.critical(self, "Error", "No PO number found")
            return 

        if voltage is None:
            self.set_light(self.lightPassorFail, "red", "ERROR")
            QMessageBox.critical(self, "Error", "DMM not connected or read failed")
            error_serial = self.get_fail_serial(self.current_serial)
            success, message = self.record_result(error_serial, 0, None, "NO READING")
            if success:
                self.write_row(
                    error_serial,
                    po_num,
                    operator,
                    shift,
                    datetime.datetime.now(),
                    None,
                    self.test_duration,
                    self._suffix_number(error_serial),
                    0,
                    "NO READING",
                )
            else:
                QMessageBox.warning(self, "Database Error", f"Failed to record error result: {message}")
            self._disarm_start()
            return

        if self.min_voltage <= voltage <= self.max_voltage:
        # if True:
            status_value = 1
            fail_reason = None
            write_serial = self.current_serial
            self.set_light(self.lightPassorFail, "green", "PASS")
            # ✅ Green bar on pass
            self.progressBar.setStyleSheet("""
                QProgressBar {
                    border: 1px solid #999;
                    border-radius: 5px;
                    text-align: center;
                }
                QProgressBar::chunk {
                    background-color: #06B025;
                }
            """)
        else:
            status_value = 0
            fail_reason = "LOW LIMIT" if voltage < self.min_voltage else "HIGH LIMIT"
            write_serial = self.get_fail_serial(self.current_serial)
            self.set_light(self.lightPassorFail, "red", "FAIL")
            # ✅ Red bar on fail (no change from before, just cleaned up)
            self.progressBar.setStyleSheet("""
                QProgressBar {
                    border: 1px solid #999;
                    border-radius: 5px;
                    text-align: center;
                }
                QProgressBar::chunk {
                    background-color: #F60000;
                }
            """)
        
        success, message = self.record_result(write_serial, status_value, voltage, fail_reason)
        if not success:
            QMessageBox.warning(self, "Database Error", f"Failed to record result: {message}")
            return

        self.write_row(
            write_serial,
            po_num,
            operator,
            shift,
            datetime.datetime.now(),
            voltage,
            self.test_duration,
            self._suffix_number(self.get_fail_serial(self.current_serial)),
            status_value,
            fail_reason,
        )

        if status_value == 1:
            update_success, update_message = self.update_nanoirtag_battery_flag(self.current_serial)
            if not update_success:
                QMessageBox.warning(self, "Database Error", f"Failed to update nanoirtag_main: {update_message}")

        # Reset armed state after every completed test
        self._disarm_start()

    def _suffix_number(self, serial):
        match = re.search(r'_(\d+)$', serial)
        return int(match.group(1)) if match else 0

    def check_po_num(self, serial_num):
        if self.test_mode == "Development":
            return True, "DEV-MODE"
        try:
            conn = pymysql.connect(
                host=self.db_host,
                user="labeling",
                password="labeling",
                database=self.schema
            )
            base_serial = serial_num.split('_')[0]
            cursor = conn.cursor()
            query = """
                SELECT po_num
                FROM """ + self.main_table + """
                WHERE serial_num LIKE %s
                """
            cursor.execute(query, (f"{base_serial}%",))
            result = cursor.fetchone()
            conn.close()

            if not result:
                QMessageBox.critical(self, "Error", "Serial not found in database")
                return False, "Serial not found in database"
            return True, result[0]
        except Exception as e:
            return False, f"DB Error: {e}"

    def has_passed_before(self, serial_num):
        if self.test_mode == "Development":
            return False
        #     if not os.path.isfile("results.csv"):
        #         return 0
        #     with open("results.csv", "r", newline="") as f:
        #         reader = csv.DictReader(f)
        #         for row in reader:
        #             serial_col = row.get("Serial", "")
        #             status_col = str(row.get("Status", "")).strip()
        #             if serial_col == serial_num and status_col == "1":
        #                 return True
        #         return False
        
        try:
            conn = pymysql.connect(
                host=self.db_host,
                user="labeling",
                password="labeling",
                database=self.schema
            )
            cursor = conn.cursor()
            query = "SELECT battery FROM " + self.main_table + " WHERE serial_num = %s"
            cursor.execute(query, (serial_num,))
            result = cursor.fetchone()
            if result and result[0] == 1:
                conn.close()
                return True
            query = "SELECT status FROM " + self.station + " WHERE serial_num = %s"
            cursor.execute(query, (serial_num,))
            result = cursor.fetchone()
            conn.close()
            return bool(result and result[0] == 1)
        except Exception:
            return False

    def check_station_status(self, serial_raw):
        serial_num = serial_raw.split('_')[0]
        if self.test_mode == "Development":
            return True, "All stations passed (Dev Mode)"
        try:
            conn = pymysql.connect(
                host=self.db_host,
                user="labeling",
                password="labeling",
                database=self.schema
            )

            cursor = conn.cursor(DictCursor)
            query = "SELECT * FROM " + self.main_table + " WHERE serial_num = %s "
            cursor.execute(query, (serial_num,))
            result_main = cursor.fetchone()
            # print(result_main)

            # if result_main is None:
            stat_header = self.station.split('_')[-1]
            if not self.previous_stations:
                for key in result_main.keys():
                    if key == stat_header:
                        break
                    if (key != 'serial_num' and key != 'po_num'):
                        self.previous_stations.append(key)
            # print(self.previous_stations)

            fail_previous = []
            for ps in self.previous_stations:
                print(f"{self.model}_{ps}")
                query = "SELECT status FROM " + f"{self.model}_{ps}" + " WHERE serial_num LIKE %s"
                cursor.execute(query, (serial_num,))
                res = cursor.fetchone()
                if res is None:
                    fail_previous.append(ps)
                elif res['status'] == 0:
                    fail_previous.append(ps)
            # print(fail_previous)
            cursor.close()
            conn.close()

            if not result_main:
                return False, "Serial not found in database"

            if len(fail_previous) > 0:
                return False, f"Did not pass {','.join(fail_previous)}"

            return True, "All stations passed"

        except Exception as e:
            return False, f"DB Error: {e}"

    def update_voltage_display(self):
        if not self.dmm or not self.dmm.is_open:
            self.voltagereader.setText("Disconnected")
            return

        voltage = self.read_voltage()
        if voltage is not None:
            self.voltagereader.setText(f"{voltage:.2f} V")
        else:
            self.voltagereader.setText("No Reading")

    def read_voltage(self):
        if not self.dmm:
            return None
        try:
            self.dmm.reset_input_buffer()
            self.dmm.reset_output_buffer()
            self.dmm.write(b'MEAS:VOLT:DC?\r\n')
            self.dmm.flush()
            response = self.dmm.readline().decode('ascii', errors='ignore').strip()
            if not response:
                response = self.dmm.read_until(expected=b'\n', size=100).decode('ascii', errors='ignore').strip()

            match = re.search(r'[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?', response)
            if not match:
                return None

            return float(match.group(0))
        except Exception:
            return None

    def record_result(self, serial_num, status, voltage, fail_reason=None):
        if self.test_mode == "Development":
            print(f"[DEV MODE] Would save: {serial_num}, voltage={voltage}, status={status}, fail_reason={fail_reason}")
            return True, "Result recorded (Dev Mode - CSV only)"
        try:
            conn = pymysql.connect(
                host=self.db_host,
                user="testing",
                password="testing",
                database=self.schema
            )
            cursor = conn.cursor()

            po_ok, po_num = self.check_po_num(serial_num)
            if not po_ok:
                return False, "No PO number"

            operator = self.operatoren.text().strip()
            shift = self.shift.currentText().strip()
            test_rep = 1

            query = """
                INSERT INTO """ + self.station + """
                (serial_num, po_num, operator_en, shift, date_time, vbat_volt, duration, test_rep, status, fail_reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    po_num       = VALUES(po_num),
                    operator_en  = VALUES(operator_en),
                    shift        = VALUES(shift),
                    date_time    = VALUES(date_time),
                    vbat_volt    = VALUES(vbat_volt),
                    duration     = VALUES(duration),
                    test_rep     = VALUES(test_rep),
                    status       = VALUES(status),
                    fail_reason  = VALUES(fail_reason)
            """

            readback = None
            for _ in range(3):
                if readback:
                    break
                cursor.execute(query, (
                    serial_num,
                    po_num,
                    operator,
                    shift,
                    datetime.datetime.now(),
                    voltage,
                    self.test_duration,
                    test_rep,
                    status,
                    fail_reason,
                ))
                conn.commit()

                readback_query = "SELECT * FROM " + self.station + " WHERE serial_num = %s"
                cursor.execute(readback_query, (serial_num,))
                readback = cursor.fetchall()
                
            conn.close()
            if readback:
                return True, "Result recorded"
            else:
                return False, "DB write failed after 3 times"
        except Exception as e:
            return False, f"DB Error: {e}"

    def update_nanoirtag_battery_flag(self, serial_num):
        if self.test_mode == "Development":
            print(f"[DEV MODE] Would update nanoirtag_main for {serial_num}")
            return True, "Updated nanoirtag_main (Dev Mode - skipped)"
        try:
            conn = pymysql.connect(
                host=self.db_host,
                user="testing",
                password="testing",
                database=self.schema
            )
            cursor = conn.cursor()
            query = """
                UPDATE """ + self.main_table + """
                SET battery = 1
                WHERE serial_num = %s
            """
            cursor.execute(query, (serial_num,))
            conn.commit()
            conn.close()
            return True, "Updated nanoirtag_main"
        except Exception as e:
            return False, f"DB Error: {e}"

    def write_row(
        self,
        serial,
        po_num=None,
        operator=None,
        shift=None,
        date_time=None,
        vbat_volt=None,
        duration=None,
        test_rep=None,
        status=None,
        fail_reason=None,
    ):
        file_exists = os.path.isfile("results.csv")
        with open("results.csv", "a", newline="") as file:
            writer = csv.writer(file)
            if not file_exists:
                writer.writerow([
                    "Serial",
                    "PO Number",
                    "Operator",
                    "Shift",
                    "DateTime",
                    "VBAT_Volt",
                    "Duration",
                    "Test_Rep",
                    "Status",
                    "Fail_Reason",
                ])
            if date_time is None:
                date_time = datetime.datetime.now()
            writer.writerow([
                serial,
                po_num,
                operator,
                shift,
                date_time,
                vbat_volt,
                duration,
                test_rep,
                status,
                fail_reason,
            ])

    def resource_path(self, relative_path):
        try:
            base_path = sys._MEIPASS
        except AttributeError:
            base_path = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base_path, relative_path)

    def update_ui(self):
        self.set_light(self.lightPassorFail, "lightgrey")

    def set_light(self, label, color, text="", heightsize=40, widthsize=100):
        label.setFixedSize(widthsize, heightsize)
        label.setText(text)
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet(f"""
            background-color: {color};
            border-radius: 5px;
            border: 2px solid #555;
            color: white;
            font-weight: bold;
            font-size: 14px;
        """)

    def logout(self):
        self.loginbutton.setEnabled(True)
        self.operatoren.setEnabled(True)
        self.shift.setEnabled(True)
        self.progressBar.setEnabled(False)
        self.serialnumber.setEnabled(False)
        self.serialnumber.setReadOnly(False)
        self.startbutton.setEnabled(False)
        self.progressBar.setValue(0)
        self.set_light(self.lightPassorFail, "lightgrey")
        self.operatoren.clear()
        self.shift.setCurrentIndex(0)
        self._disarm_start()

    def quit(self):
        self.message_form.close()


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    window = BatteryTestApp()
    window.show()
    sys.exit(app.exec_())