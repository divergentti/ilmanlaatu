""""

This script is used for airquality measurement. Display is ILI9341 2.8" TFT touch screen in the SPI bus,
CO2 device is MH-Z19 NDIR-sensor, particle sensor is PMS7003 and temperature/rh/pressure sensor BME280.

Boot.py does not setup network. If user wants to connect to the network and to the mqtt-server, that can be done
from the display either with predefined SSID name and password, or password for accessible networks.


Sensor datasheets:
PMS7003 https://download.kamami.com/p564008-p564008-PMS7003%20series%20data%20manua_English_V2.5.pdf


Libraries:
1. MQTT_AS https://github.com/peterhinch/micropython-mqtt/blob/master/mqtt_as/mqtt_as.py
2. MHZ19B_AS.py in this blob. Note: UART2 initialization problems!
   - RX pin goes to sensor TX ping and vice versa
   - Use 5V
3. ILI9341 display, touchscreen, fonts and keyboard https://github.com/rdagger/micropython-ili9341
   You may drive display LED from GPIO if display has transistor in the LED pin. Otherwise connect LED to 3.3V
4. PMS7003_AS.py in this blob. Modified async from https://github.com/pkucmus/micropython-pms7003/blob/master/pms7003.py
   - Use 3.3V instead of 5V!
5. AQI.py from https://github.com/pkucmus/micropython-pms7003/blob/master/aqi.py

!! DO NOT USE PyCharm to upload fonts or images to the ESP32! Use command ampy -p COMx directoryname instead!
   PyCharm can not handle directories in ESP32. For some reason it combines name of the directory and file to one file.


13.01.2020: Jari Hiltunen
14.01.2020: Network part shall be ok if parameters.py used and communicates with the display.
15.01.2020: Added some welcome stuff and fixed SPI buss speed so that touchscreen and keyboard works ok.
16.01.2020: Added PMS7003 particle sensor asynchronous reading.
            Sensor returns a dictionary, which is passed to the simple air quality calculation
17.01.2020: Added status update loop which turn screen red if over limits and formatted display.
            Fixed UART2 (CO2 sensor) related problem after power on boot by deleting sensor object and recreating it
            again. No idea why UART2 does not work after power on boot but works after soft etc boots.
            Fixed also MHZ19 class reading so that values can not be more than sensor range.
19.01.2020: Added three first screens and logic for colour change if values over limit.
20.01.2020: Added text boxes to the display for setting up network, IOT, display and Debug

This code is in its very beginning steps!

"""
from machine import SPI, Pin, reset, freq, reset_cause, Timer
import uasyncio as asyncio
import utime
import gc
from MQTT_AS import MQTTClient, config
import network
import ntptime
import webrepl
from XPT2046 import Touch
from ILI9341 import Display, color565
from XGLCD_FONT import XglcdFont
from TOUCH_KEYBOARD import TouchKeyboard
from AQI import AQI
import PMS7003_AS as PARTICLES
import MHZ19B_AS as CO2


try:
    f = open('parameters.py', "r")
    from parameters import SSID1, SSID2, PASSWORD1, PASSWORD2, MQTT_SERVER, MQTT_PASSWORD, MQTT_USER, MQTT_PORT, \
        CLIENT_ID, TOPIC_ERRORS, CO2_SENSOR_RX_PIN, CO2_SENSOR_TX_PIN, CO2_SENSOR_UART, TFT_CS_PIN, TFT_DC_PIN, \
        TFT_TOUCH_MISO_PIN, TFT_TOUCH_CS_PIN, TFT_TOUCH_IRQ_PIN, TFT_TOUCH_MOSI_PIN, TFT_TOUCH_SCLK_PIN, TFT_CLK_PIN, \
        TFT_RST_PIN, TFT_MISO_PIN, TFT_MOSI_PIN, TFT_SPI, TOUCHSCREEN_SPI, WEBREPL_PASSWORD, NTPSERVER, DHCP_NAME, \
        PARTICLE_SENSOR_UART, PARTICLE_SENSOR_TX, PARTICLE_SENSOR_RX
except OSError:  # open failed
    print("parameter.py-file missing! Can not continue!")
    raise


def restart_and_reconnect():
    #  Last resort
    print("About to reboot in 20s... ctrl + c to break")
    utime.sleep(20)
    reset()


def resolve_date():
    (year, month, mdate, hour, minute, second, wday, yday) = utime.localtime()
    """ Simple DST for Finland """
    summer_march = utime.mktime((year, 3, (14 - (int(5 * year / 4 + 1)) % 7), 1, 0, 0, 0, 0, 0))
    winter_december = utime.mktime((year, 10, (7 - (int(5 * year / 4 + 1)) % 7), 1, 0, 0, 0, 0, 0))
    if utime.mktime(utime.localtime()) < summer_march:
        dst = utime.localtime(utime.mktime(utime.localtime()) + 7200)
    elif utime.mktime(utime.localtime()) < winter_december:
        dst = utime.localtime(utime.mktime(utime.localtime()) + 7200)
    else:
        dst = utime.localtime(utime.mktime(utime.localtime()) + 10800)
    (year, month, mdate, hour, minute, second, wday, yday) = dst
    day = "%s.%s.%s" % (mdate, month, year)
    time = "%s:%s:%s" % ("{:02d}".format(hour), "{:02d}".format(minute), "{:02d}".format(second))
    return day, time


async def error_reporting(error):
    # error message: date + time;uptime;devicename;ip;error;free mem
    errormessage = str(resolve_date()) + ";" + str(utime.ticks_ms()) + ";" \
        + str(CLIENT_ID) + ";" + str(network.WLAN(network.STA_IF).ifconfig()) + ";" + str(error) +\
        ";" + str(gc.mem_free())
    # await client.publish(TOPIC_ERRORS, str(errormessage), retain=False)


class ConnectWiFi(object):
    """ This class creates network object for WiFi-connection. SSID may be defined in the parameters.py or
    user may input a password, which is tried to WiFi APs within range. """

    def __init__(self, displayin):
        #  Check if we are already connected
        self.ip_address = None
        self.wifi_strenth = None
        self.timeset = False
        self.webrepl_started = False
        self.searh_list = []
        self.ssid_list = []
        self.mqttclient = None
        self.display = displayin
        self.display.setup_screen_active = True
        if network.WLAN(network.STA_IF).config('essid') != '':
            self.display.row_by_row_text("Connected to network %s" % network.WLAN(network.STA_IF).config('essid'),
                                         'fuschia')
            self.use_ssid = network.WLAN(network.STA_IF).config('essid')
            self.display.row_by_row_text(('IP-address: %s' % network.WLAN(network.STA_IF).ifconfig()[0]), 'fuschia')
            self.ip_address = network.WLAN(network.STA_IF).ifconfig()[0]
            self.display.row_by_row_text("WiFi-signal strength %s" % (network.WLAN(network.STA_IF).status('rssi')),
                                         'fuschia')
            self.wifi_strenth = network.WLAN(network.STA_IF).status('rssi')
            self.network_connected = True
            self.display.setup_screen_active = False
            self.use_ssid = network.WLAN(network.STA_IF).config('essid')
            # resolve is essid in the predefined networks presented in parameters.py
            if self.use_ssid == SSID1:
                self.use_password = PASSWORD1
                self.predefined = True
            elif self.use_ssid == SSID2:
                self.use_password = PASSWORD2
                self.predefined = True
        else:
            # We are not connected, check if SSID1 or SSID2 is predefined
            if (SSID1 is not None) or (SSID2 is not None):
                self.predefined = True
            else:
                self.predefined = False
            self.password = None
            self.use_password = None    # Password may be from parameters.py or from user input
            self.use_ssid = None   # SSID to be used will be decided later even it is in the parameters.py
            self.network_connected = False
            self.search_wifi_networks()

    def start_webrepl(self):
        if self.webrepl_started is False:
            if WEBREPL_PASSWORD is not None:
                try:
                    webrepl.start(password=WEBREPL_PASSWORD)
                    self.webrepl_started = True
                except OSError:
                    pass
            else:
                try:
                    webrepl.start()
                    self.webrepl_started = True
                except OSError as e:
                    print("WebREPL do not start. Error %s" % e)
                    self.display.row_by_row_text("WebREPL do not start. Error %s" % e, 'red')
                    return False

    def set_time(self):
        if self.timeset is False:
            try:
                ntptime.settime()
                self.timeset = True
            except OSError as e:
                self.display.row_by_row_text("No time from NTP server %s! Error %s" % (NTPSERVER, e), 'red')
                self.timeset = False
                return False
            self.display.row_by_row_text("Time: %s " % str(utime.localtime(utime.time())), 'white')
            self.display.setup_screen_active = False

    def search_wifi_networks(self):
        # Begin with adapter reset
        network.WLAN(network.STA_IF).active(False)
        utime.sleep(2)
        network.WLAN(network.STA_IF).active(True)
        utime.sleep(3)
        if DHCP_NAME is not None:
            network.WLAN(network.STA_IF).config(dhcp_hostname=DHCP_NAME)
        if NTPSERVER is not None:
            ntptime.host = NTPSERVER
        self.display.row_by_row_text("Check what hotspots we see", 'green')
        try:
            # Generate list of WiFi hotspots in range
            self.ssid_list = network.WLAN(network.STA_IF).scan()
            utime.sleep(5)
        except self.ssid_list == []:
            print("No WiFi-networks within range!")
            self.display.row_by_row_text("No WiFi-networks within range!", 'red')
            utime.sleep(10)
        except OSError:
            return False

        if len(self.ssid_list) > 0:
            self.display.row_by_row_text("Found following hotspots:", 'green')
            for i in self.ssid_list:
                display.row_by_row_text(i[0].decode(), 'white')

        if self.predefined is True:
            #  Network to be connected is in the parameters.py. Check if SSID1 or SSID2 is in the list
            self.display.row_by_row_text("Checking predefined networks...", 'white')
            try:
                self.searh_list = [item for item in self.ssid_list if item[0].decode() == SSID1 or
                                   item[0].decode() == SSID2]
            except ValueError:
                # SSDI not found within signal range
                self.display.row_by_row_text("Parameters.py SSIDs not found in the signal range!", 'red')
                utime.sleep(10)
                return False
            # If both are found, select one which has highest stregth
            if len(self.searh_list) == 2:
                #  third from end of list is rssi
                if self.searh_list[0][-3] > self.searh_list[1][-3]:
                    self.use_ssid = self.searh_list[0][0].decode()
                    self.use_password = PASSWORD1
                    self.display.row_by_row_text("Using hotspot: %s" % self.use_ssid, 'yellow')
                else:
                    self.use_ssid = self.searh_list[1][0].decode()
                    self.use_password = PASSWORD2
                    self.display.row_by_row_text("Using hotspot: %s" % self.use_ssid, 'yellow')
            else:
                # only 1 in the list
                self.use_ssid = self.searh_list[0][0].decode()
                self.use_password = PASSWORD1
                self.display.row_by_row_text("Using hotspot: %s" % self.use_ssid, 'yellow')

        if self.predefined is False:
            #  Networks not defined in the parameters.py, let's try password to any WiFi order by signal strength
            #  Tries empty password too
            #  ToDo: rebuild, ask user input which hotspot we want to connect!
            self.ssid_list.sort(key=lambda x: [x][-3])
            if len(self.ssid_list) == 1:
                self.use_ssid = self.ssid_list[0][0].decode()
            elif len(self.ssid_list) > 1:
                z = 0
                while (network.WLAN(network.STA_IF).ifconfig()[0] == '0.0.0.0') and (z <= len(self.ssid_list)) and \
                        (self.network_connected is False):
                    self.use_ssid = self.ssid_list[z][0].decode()
                    z = +1

    async def connect_to_network(self):
        #  We know which network we should connect to, but shall we connect?
        self.display.setup_screen_active = True
        self.display.row_by_row_text("Connecting to AP %s ..." % self.use_ssid, 'green')
        try:
            network.WLAN(network.STA_IF).connect(self.use_ssid, self.use_password)
            utime.sleep(10)
        except network.WLAN(network.STA_IF).ifconfig()[0] == '0.0.0.0':
            self.display.row_by_row_text("No IP address!", 'red')
            utime.sleep(5)
            return False
        except OSError:
            pass
        finally:
            if network.WLAN(network.STA_IF).ifconfig()[0] != '0.0.0.0':
                self.set_time()
                self.start_webrepl()
                self.display.row_by_row_text("Connected to network %s" % network.WLAN(network.STA_IF).config('essid'),
                                             'green')
                self.use_ssid = network.WLAN(network.STA_IF).config('essid')
                self.display.row_by_row_text(('IP-address: %s' % network.WLAN(network.STA_IF).ifconfig()[0]), 'green')
                self.ip_address = network.WLAN(network.STA_IF).ifconfig()[0]
                self.display.row_by_row_text("WiFi-signal strength %s" % (network.WLAN(network.STA_IF).status('rssi')),
                                             'green')
                self.wifi_strenth = network.WLAN(network.STA_IF).status('rssi')
                self.network_connected = True
                self.display.setup_screen_active = False
                return True
            else:
                self.display.row_by_row_text("No network connection! Soft rebooting in 10s...", 'red')
                self.network_connected = False
                utime.sleep(10)
                reset()

    def mqtt_init(self):
        config['server'] = MQTT_SERVER
        config['ssid'] = self.use_ssid
        config['wifi_pw'] = self.use_password
        config['user'] = MQTT_USER
        config['password'] = MQTT_PASSWORD
        config['port'] = MQTT_PORT
        config['client_id'] = CLIENT_ID
        config['subs_cb'] = self.update_mqtt_status
        config['connect_coro'] = self.mqtt_subscribe
        # Communication object
        self.mqttclient = MQTTClient(config)

    async def mqtt_up_loop(self):
        #  This loop just keeps the mqtt connection up
        await self.mqtt_subscribe()
        n = 0
        while True:
            await asyncio.sleep(5)
            print('mqtt-publish', n)
            await self.mqttclient.publish('result', '{}'.format(n), qos=1)
            n += 1

    async def mqtt_subscribe(self):
        await asyncio.sleep(1)
        # await self.mqttclient.subscribe(TOPIC_OUTDOOR, 0)

    def update_mqtt_status(self, topic, msg, retained):
        # print("Topic: %s, message %s" % (topic, msg))
        status = int(msg)
        if status == 1:
            print("daa")
        elif status == 0:
            print("kaa")


class TFTDisplay(object):

    def __init__(self, touchspi, dispspi):

        # Display - some digitizers may be rotated 270 degrees!
        self.display = Display(spi=dispspi, cs=Pin(TFT_CS_PIN), dc=Pin(TFT_DC_PIN), rst=Pin(TFT_RST_PIN),
                               width=320, height=240, rotation=90)

        # Default fonts
        self.unispace = XglcdFont('fonts/Unispace12x24.c', 12, 24)
        self.fixedfont = XglcdFont('fonts/FixedFont5x8.c', 5, 8, 32, 96)
        self.arcadepix = XglcdFont('fonts/ArcadePix9x11.c', 9, 11)
        self.active_font = self.unispace

        self.colours = {'red': color565(255, 0, 0), 'green': color565(0, 255, 0), 'blue': color565(0, 0, 255),
                        'yellow': color565(255, 255, 0), 'fuschia': color565(255, 0, 255),
                        'aqua': color565(0, 255, 255), 'maroon': color565(128, 0, 0),
                        'darkgreen': color565(0, 128, 0), 'navy': color565(0, 0, 128),
                        'teal': color565(0, 128, 128), 'purple': color565(128, 0, 128),
                        'olive': color565(128, 128, 0), 'orange': color565(255, 128, 0),
                        'deep_pink': color565(255, 0, 128), 'charteuse': color565(128, 255, 0),
                        'spring_green': color565(0, 255, 128), 'indigo': color565(128, 0, 255),
                        'dodger_blue': color565(0, 128, 255), 'cyan': color565(128, 255, 255),
                        'pink': color565(255, 128, 255), 'light_yellow': color565(255, 255, 128),
                        'light_coral': color565(255, 128, 128), 'light_green': color565(128, 255, 128),
                        'white': color565(255, 255, 255), 'black': color565(0, 0, 0)}

        self.colour_fonts = 'white'
        self.colour_background = 'light_green'

        # Touchscreen
        self.xpt = Touch(spi=touchspi, cs=Pin(TFT_TOUCH_CS_PIN), int_pin=Pin(TFT_TOUCH_IRQ_PIN),
                         width=240, height=320, x_min=100, x_max=1962, y_min=100, y_max=1900)
        self.xpt.int_handler = self.first_press
        self.rownumber = 1
        self.rowheight = 10
        self.fontheight = 10
        self.fontwidth = 10
        self.maxrows = self.display.height / self.fontheight
        self.max_x = self.display.width - 1
        self.max_y = self.display.height - 1
        self.textbox1_x = 0
        self.textbox1_y = 0
        self.textbox1_w = int(self.max_x / 2)
        self.textbox1_h = int(self.max_y / 2)

        self.textbox2_x = int(self.max_x / 2)
        self.textbox2_y = 0
        self.textbox2_w = int(self.max_x / 2)
        self.textbox2_h = int(self.max_y / 2)

        self.textbox3_x = 0
        self.textbox3_y = int(self.max_y / 2)
        self.textbox3_w = int(self.max_x / 2)
        self.textbox3_h = int(self.max_y / 2)

        self.textbox4_x = int(self.max_x / 2)
        self.textbox4_y = int(self.max_y / 2)
        self.textbox4_w = int(self.max_x / 2)
        self.textbox4_h = int(self.max_y / 2)

        self.leftindent_pixels = 12
        self.diag_count = 0
        self.screen_timeout = False
        self.timeout_timer = Timer(-1)
        self.keyboard = None
        self.keyboard_show = False
        # If all ok is False, change background color etc
        self.all_ok = True
        self.screen_update_interval = 5
        #  To avoid duplicate screens
        self.setup_screen_active = False
        # TODO test normally controlled from network setup screen
        self.connect_to_wifi = True

    def try_to_connect(self, ssid, pwd):
        """ Return WiFi connection status.
        Args:
            pwd: password for the SSID
        Returns:
            status of the connection.
        """
        status = 0
        pass

        return status

    def first_press(self, x, y):
        """ If touchscreen is pressed, draw first setup screen """
        gc.collect()
        # self.display.clear()
        self.keyboard_show = False
        self.setup_screen_active = True
        self.draw_setup_screen()

    def draw_setup_screen(self):
        """ Split screen to 4 divisions, network setup, iot setup, display setup and debug setup  """
        self.active_font = self.unispace
        self.fontheight = self.active_font.height
        self.fontwidth = self.active_font.width

        textbox1_1 = "Network"
        textbox1_1_mid_x = self.textbox1_x + (int(self.textbox1_w / 2) - (int((len(textbox1_1) * self.fontwidth)/2)))
        textbox1_1_mid_y = self.textbox1_y + int(self.textbox1_h/2)
        textbox2_1 = "IOT"
        textbox2_1_mid_x = self.textbox2_x + (int(self.textbox2_w / 2) - (int((len(textbox2_1) * self.fontwidth)/2)))
        textbox2_1_mid_y = self.textbox2_y + int(self.textbox2_h/2)
        textbox3_1 = "Display"
        textbox3_1_mid_x = self.textbox3_x + (int(self.textbox3_w / 2) - (int((len(textbox3_1) * self.fontwidth)/2)))
        textbox3_1_mid_y = self.textbox3_y + int(self.textbox3_h/2)
        textbox4_1 = "Debug"
        textbox4_1_mid_x = self.textbox4_x + (int(self.textbox4_w / 2) - (int((len(textbox4_1) * self.fontwidth)/2)))
        textbox4_1_mid_y = self.textbox4_y + int(self.textbox4_h/2)

        # For network setup
        self.display.fill_rectangle(self.textbox1_x, self.textbox1_y, self.textbox1_w, self.textbox1_h,
                                    self.colours['blue'])
        self.display.draw_text(textbox1_1_mid_x, textbox1_1_mid_y, textbox1_1, self.active_font,
                               self.colours['white'], self.colours['blue'])
        # Is network up or down?
        if wifinet.network_connected is True:
            self.display.draw_text(self.textbox1_x + 2, self.textbox1_y + 2, "Network up",
                                   self.fixedfont, self.colours['green'])
        else:
            self.display.draw_text(self.textbox1_x + 2, self.textbox1_y + 2, "Network down", self.fixedfont,
                                   self.colours['purple'])

        # For IOT Setup
        self.display.fill_rectangle(self.textbox2_x, self.textbox2_y, self.textbox2_w, self.textbox2_h,
                                    self.colours['yellow'])
        self.display.draw_text(textbox2_1_mid_x, textbox2_1_mid_y, textbox2_1, self.active_font,
                               self.colours['green'], self.colours['yellow'])

        # For Display setup
        self.display.fill_rectangle(self.textbox3_x, self.textbox3_y, self.textbox3_w, self.textbox3_h,
                                    self.colours['light_green'])
        self.display.draw_text(textbox3_1_mid_x, textbox3_1_mid_y, textbox3_1, self.active_font,
                               self.colours['light_green'], self.colours['white'])

        # For Debug setup
        self.display.fill_rectangle(self.textbox4_x, self.textbox4_y, self.textbox4_w, self.textbox4_h,
                                    self.colours['light_yellow'])
        self.display.draw_text(textbox4_1_mid_x, textbox4_1_mid_y, textbox4_1, self.active_font,
                               self.colours['red'], self.colours['light_yellow'])

        # Replace init handel to check what user choose
        self.xpt.int_handler = self.select_setup_box

    def select_setup_box(self, x, y):
        # Init handler for setup screen
        print("Print select setup %s %s" % (x, y))
        # Check which box was pressed
        box = 0
        if (x > self.textbox1_x) and (x < self.textbox2_x):
            # left part
            box = 13    # box 1 or 3
        elif x > self.textbox2_x:
            # right part
            box = 24    # box 2 or 4
        if box == 13:
            if y > self.textbox3_h:
                print("IOT chosen")
            else:
                print("Network chosen")
        if box == 24:
            if y > self.textbox4_h:
                print("Debug chosen")
            else:
                print("Display chosen")

    def activate_keyboard(self, x, y):
        #  Setup keyboard
        self.keyboard = TouchKeyboard(self.display, self.unispace)
        self.keyboard_show = True

        """Process touchscreen press events. Disable debug if you do not want to see green circle on the keyboard """
        # TODO: Capture characters and enter
        if self.keyboard.handle_keypress(x, y, debug=False) is True:
            self.keyboard.locked = True
            pwd = self.keyboard.kb_text
            self.keyboard.show_message("Type password", color565(0, 0, 255))
            try:
                status = self.try_to_connect(pwd)
                if status:
                    # Connection established
                    msg = "Connection established!: {0}".format(status)
                    self.keyboard.show_message(msg, color565(255, 0, 0))
                else:
                    # Connection not established
                    msg = "No connection. Try another password!"
                    self.keyboard.show_message(msg, color565(0, 255, 0))
            except Exception as e:
                if hasattr(e, 'message'):
                    self.keyboard.show_message(e.message[:22],
                                               color565(255, 255, 255))
                else:
                    self.keyboard.show_message(str(e)[:22],
                                               color565(255, 255, 255))
            self.keyboard.waiting = True
            self.keyboard.locked = False
            self.keyboard_show = False

    def row_by_row_text(self, message, color):
        self.active_font = self.arcadepix
        self.fontheight = self.active_font.height
        self.rowheight = self.fontheight + 2  # 2 pixel space between rows
        self.display.draw_text(5, self.rowheight * self.rownumber, message, self.arcadepix, self.colours[color])
        self.rownumber += 1
        if self.rownumber >= self.maxrows:
            utime.sleep(self.screen_update_interval)
            # TODO: scrolling screen!
            # self.display.cleanup()
            self.rownumber = 1

    async def run_display_loop(self):
        # TODO: Initial welcome screen?

        # await self.show_welcome_screen()

        # NOTE: Loop is started in the main()

        while True:
            if self.setup_screen_active is False:
                rows, rowcolours = await self.show_time_co2_temp_screen()
                await self.show_screen(rows, rowcolours)
                rows, rowcolours = await self.show_particle_screen()
                await self.show_screen(rows, rowcolours)
                rows, rowcolours = await self.show_status_monitor_screen()
                await self.show_screen(rows, rowcolours)
            else:
                await asyncio.sleep(1)

    async def show_screen(self, rows, rowcolours):
        row1 = "Airquality 0.02"
        row1_colour = 'white'
        row2 = "."
        row2_colour = 'white'
        row3 = "Wait"
        row3_colour = 'white'
        row4 = "For"
        row4_colour = 'white'
        row5 = "Values"
        row5_colour = 'white'
        row6 = "."
        row6_colour = 'white'
        row7 = "Wait"
        row7_colour = 'white'

        if rows is not None:
            if len(rows) == 7:
                row1, row2, row3, row4, row5, row6, row7 = rows
            if len(rowcolours) == 7:
                row1_colour, row2_colour, row3_colour, row4_colour, row5_colour, row6_colour, row7_colour = rowcolours

        if (self.keyboard_show is False) and (self.setup_screen_active is False):
            if self.all_ok is True:
                await self.draw_all_ok_background()
            else:
                await self.draw_error_background()
            self.active_font = self.unispace
            self.fontheight = self.active_font.height
            self.rowheight = self.fontheight + 2  # 2 pixel space between rows
            self.display.draw_text(self.leftindent_pixels, 25, row1, self.active_font, self.colours[row1_colour],
                                   self.colours[self.colour_background])
            self.display.draw_text(self.leftindent_pixels, 25 + self.rowheight, row2, self.active_font,
                                   self.colours[row2_colour], self.colours[self.colour_background])
            self.display.draw_text(self.leftindent_pixels, 25 + self.rowheight * 2, row3, self.active_font,
                                   self.colours[row3_colour], self.colours[self.colour_background])
            self.display.draw_text(self.leftindent_pixels, 25 + self.rowheight * 3, row4, self.active_font,
                                   self.colours[row4_colour], self.colours[self.colour_background])
            self.display.draw_text(self.leftindent_pixels, 25 + self.rowheight * 4, row5, self.active_font,
                                   self.colours[row5_colour], self.colours[self.colour_background])
            self.display.draw_text(self.leftindent_pixels, 25 + self.rowheight * 5, row6, self.active_font,
                                   self.colours[row6_colour], self.colours[self.colour_background])
            self.display.draw_text(self.leftindent_pixels, 25 + self.rowheight * 6, row7, self.active_font,
                                   self.colours[row7_colour], self.colours[self.colour_background])
            await asyncio.sleep(self.screen_update_interval)

    async def draw_all_ok_background(self):
        self.display.fill_rectangle(0, 0, self.display.width, self.display.height, self.colours['yellow'])
        # TODO: replace excact values to display size values
        self.display.fill_rectangle(10, 10, 300, 220, self.colours['light_green'])
        self.colour_background = 'light_green'

    async def draw_error_background(self):
        self.display.fill_rectangle(0, 0, self.display.width, self.display.height, self.colours['red'])
        self.display.fill_rectangle(10, 10, 300, 220, self.colours['orange'])
        self.colour_background = 'orange'

    @staticmethod
    async def show_time_co2_temp_screen():
        row1 = "%s %s" % resolve_date()
        row1_colour = 'white'
        row2 = " "
        row2_colour = 'white'
        # To avoid nonetype errors
        if co2sensor.co2_value is None:
            row3 = "CO2: waiting..."
            row3_colour = 'yellow'
        elif co2sensor.co2_average is None:
            row3 = "CO2 average counting..."
            row3_colour = 'yellow'
        else:
            row3 = "CO2: %s ppm (%s)" % ("{:.1f}".format(co2sensor.co2_value),
                                         "{:.1f}".format(co2sensor.co2_average))
            row3_colour = 'white'
        if airquality.aqinndex is None:
            row4 = "AirQuality not ready"
            row4_colour = 'yellow'
        else:
            row4 = "Air Quality Index: %s" % ("{:.1f}".format(airquality.aqinndex))
            row4_colour = 'white'
        row5 = "Temp: "
        row5_colour = 'white'
        row6 = "Rh: "
        row6_colour = 'white'
        row7 = "Pressure: %s" % gc.mem_free()
        row7_colour = 'white'
        rows = row1, row2, row3, row4, row5, row6, row7
        row_colours = row1_colour, row2_colour, row3_colour, row4_colour, row5_colour, row6_colour, row7_colour
        return rows, row_colours

    @staticmethod
    async def show_particle_screen():
        if pms.pms_dictionary is not None:
            row1 = "1. Concentration ug/m3:"
            row1_colour = 'white'
            if (pms.pms_dictionary['PM1_0'] is not None) and (pms.pms_dictionary['PM1_0_ATM'] is not None) and \
                    (pms.pms_dictionary['PM2_5'] is not None) and (pms.pms_dictionary['PM2_5_ATM'] is not None):
                row2 = " PM1:%s (%s) PM2.5:%s (%s)" % (pms.pms_dictionary['PM1_0'],
                                                       pms.pms_dictionary['PM1_0_ATM'],
                                                       pms.pms_dictionary['PM2_5'],
                                                       pms.pms_dictionary['PM2_5_ATM'])
                row2_colour = 'white'
            else:
                row2 = " Waiting"
                row2_colour = 'yellow'
            if (pms.pms_dictionary['PM10_0'] is not None) and (pms.pms_dictionary['PM10_0_ATM'] is not None):
                row3 = " PM10: %s (ATM: %s)" % (pms.pms_dictionary['PM10_0'], pms.pms_dictionary['PM10_0_ATM'])
                row3_colour = 'white'

            else:
                row3 = "Waiting"
                row3_colour = 'yellow'
            row4 = "2. Particle count/1L/um:"
            row4_colour = 'white'
            if (pms.pms_dictionary['PCNT_0_3'] is not None) and (pms.pms_dictionary['PCNT_0_5'] is not None):
                row5 = " %s < 0.3 & %s <0.5 " % (pms.pms_dictionary['PCNT_0_3'], pms.pms_dictionary['PCNT_0_5'])
                row5_colour = 'white'
            else:
                row5 = " Waiting"
                row5_colour = 'yellow'
            if (pms.pms_dictionary['PCNT_1_0'] is not None) and (pms.pms_dictionary['PCNT_2_5'] is not None):
                row6 = " %s < 1.0 & %s < 2.5" % (pms.pms_dictionary['PCNT_1_0'], pms.pms_dictionary['PCNT_2_5'])
                row6_colour = 'white'
            else:
                row6 = "Waiting"
                row6_colour = 'yellow'
            if (pms.pms_dictionary['PCNT_5_0'] is not None) and (pms.pms_dictionary['PCNT_10_0'] is not None):
                row7 = " %s < 5.0 & %s < 10.0" % (pms.pms_dictionary['PCNT_5_0'], pms.pms_dictionary['PCNT_10_0'])
                row7_colour = 'white'
            else:
                row7 = " Waiting"
                row7_colour = 'yellow'
            rows = row1, row2, row3, row4, row5, row6, row7
            row_colours = row1_colour, row2_colour, row3_colour, row4_colour, row5_colour, row6_colour, row7_colour
            return rows, row_colours
        else:
            return None

    async def show_network_screen(self):
        pass

    async def show_trends_screen(self):
        pass

    @staticmethod
    async def show_status_monitor_screen():
        row1 = "Memory free: %s" % gc.mem_free()
        row1_colour = 'white'
        row2 = "CPU ticks: %s" % utime.ticks_cpu()
        row2_colour = 'white'
        row3 = "WiFi Strenth: %s" % wifinet.wifi_strenth
        row3_colour = 'white'
        row4 = "MHZ19B CRC errors: %s " % co2sensor.crc_errors
        row4_colour = 'white'
        row5 = "MHZ19B Range errors: %s" % co2sensor.range_errors
        row5_colour = 'white'
        row6 = "PMS7003 version %s" % pms.pms_dictionary['VERSION']
        row6_colour = 'white'
        row7 = " "
        row7_colour = 'white'
        rows = row1, row2, row3, row4, row5, row6, row7
        row_colours = row1_colour, row2_colour, row3_colour, row4_colour, row5_colour, row6_colour, row7_colour
        return rows, row_colours

    async def show_display_sleep_screen(self):
        pass

    async def show_network_setup_screen(self):
        pass


class AirQuality(object):

    def __init__(self, pmssensor):
        self.aqinndex = None
        self.pms = pmssensor
        self.update_interval = 5

    async def update_airqualiy_loop(self):
        while True:
            if self.pms.pms_dictionary is not None:
                if (self.pms.pms_dictionary['PM2_5_ATM'] != 0) and (self.pms.pms_dictionary['PM10_0_ATM'] != 0):
                    self.aqinndex = (AQI.aqi(self.pms.pms_dictionary['PM2_5_ATM'],
                                             self.pms.pms_dictionary['PM10_0_ATM']))
            # TODO: control update intervals
            await asyncio.sleep(self.update_interval)


async def collect_carbage_and_update_status_loop():
    #  This sub will update status flags of objects and collect carbage
    while True:
        # For network
        if wifinet.network_connected is True:
            wifinet.use_ssid = network.WLAN(network.STA_IF).config('essid')
            wifinet.ip_address = network.WLAN(network.STA_IF).ifconfig()[0]
            wifinet.wifi_strenth = network.WLAN(network.STA_IF).status('rssi')

        # For display
        if co2sensor.co2_average is not None:
            if co2sensor.co2_average >= 1200:
                display.all_ok = False
            elif co2sensor.co2_average < 1200:
                display.all_ok = True
        if airquality.aqinndex is not None:
            if airquality.aqinndex >= 50:
                display.all_ok = False
            elif airquality.aqinndex < 50:
                display.all_ok = True
        else:
            display.all_ok = True
        gc.collect()
        #  Shall be shorter than display update interval!
        await asyncio.sleep(display.screen_update_interval - 1)


async def show_what_i_do():
    MQTTClient.DEBUG = False

    while True:
        print("PMS dictionary: %s" % pms.pms_dictionary)
        print("Air quality index: %s " % airquality.aqinndex)
        print("CO2: %s" % co2sensor.co2_value)
        print("Average: %s" % co2sensor.co2_average)
        print("-------")
        await asyncio.sleep(2)


# Kick in some speed, max 240000000, normal 160000000, min with WiFi 80000000
freq(240000000)

# Sensor and controller objects
# Particle sensor
pms = PARTICLES.PSensorPMS7003(uart=PARTICLE_SENSOR_UART, rxpin=PARTICLE_SENSOR_RX, txpin=PARTICLE_SENSOR_TX)
airquality = AirQuality(pms)
# CO2 sensor
co2sensor = CO2.MHZ19bCO2(uart=CO2_SENSOR_UART, rxpin=CO2_SENSOR_RX_PIN, txpin=CO2_SENSOR_TX_PIN)

#  If you use UART2, you have to delete object and re-create it after power on boot!
if reset_cause() == 1:
    del co2sensor
    utime.sleep(5)
    co2sensor = CO2.MHZ19bCO2(uart=CO2_SENSOR_UART, rxpin=CO2_SENSOR_RX_PIN, txpin=CO2_SENSOR_TX_PIN)

# Display and touchscreen
touchscreenspi = SPI(TOUCHSCREEN_SPI)  # HSPI
# Keep touchscreen baudrate low! If it is too high, you will get wrong values! Do not exceed 2MHz or go below 1MHz
# Might be related to S/NR of the cabling and connectors
touchscreenspi.init(baudrate=1100000, sck=Pin(TFT_TOUCH_SCLK_PIN), mosi=Pin(TFT_TOUCH_MOSI_PIN),
                    miso=Pin(TFT_TOUCH_MISO_PIN))
displayspi = SPI(TFT_SPI)  # VSPI - baudrate 40 - 90 MHz appears to be working, screen update still slow
displayspi.init(baudrate=40000000, sck=Pin(TFT_CLK_PIN), mosi=Pin(TFT_MOSI_PIN), miso=Pin(TFT_MISO_PIN))

# Initialize display object prior to wifinet-object!
display = TFTDisplay(touchscreenspi, displayspi)
wifinet = ConnectWiFi(display)


async def main():
    gc.collect()
    """ try:
        await client.connect()
    except OSError as ex:
        print("Error %s. Perhaps mqtt username or password is wrong or missing or broker down?" % ex)
        raise
    # asyncio.create_task(mqtt_up_loop()) """
    # Create all loops here, not within object classes!
    loop = asyncio.get_event_loop()
    loop.create_task(pms.read_async_loop())  # read sensor
    loop.create_task(co2sensor.read_co2_loop())   # read sensor
    loop.create_task(airquality.update_airqualiy_loop())   # calculates continuously aqi
    loop.create_task(collect_carbage_and_update_status_loop())  # updates alarm flags on display too
    # loop.create_task(show_what_i_do())    # output REPL
    loop.create_task(display.run_display_loop())   # start display show
    gc.collect()

    if (display.connect_to_wifi is True) and (wifinet.network_connected is False):
        await wifinet.connect_to_network()

    """ 
     if wifinet.network_connected is True:
        display.row_by_row_text("Network up, begin operations", 'blue')
        await display.run_display()
    else:
        display.row_by_row_text("Network down, running setup", 'yellow')
        await display.show_network_setup_screen() 
    """

    # loop.run_forever()

    while True:

        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
