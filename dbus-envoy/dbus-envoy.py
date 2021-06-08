#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""dbus-envoy.py: Driver to integrate the Enphase Envoy pv micro-inverters 
        with Victron Venus OS. """
# requires: pip install pyyaml prometheus_client

__author__      = "github usernames: jaedog"
__copyright__   = "Copyright 2020"
__license__     = "MIT"
__version__     = "0.1"

# original scrape.py:
# https://github.com/petercable/solar-observatory/
#
# info on getting live local envoy data (including generating unique envoy password):
# https://thecomputerperson.wordpress.com/2016/08/03/enphase-envoy-s-data-scraping/

import os
import sys
import signal
import time
from datetime import datetime, timedelta
import json
import requests
import threading
import logging
import yaml
from requests.auth import HTTPDigestAuth
from prometheus_client import start_http_server, Gauge

from dbus.mainloop.glib import DBusGMainLoop
import dbus
import gobject

# Victron packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'velib_python'))
from vedbus import VeDbusService
from ve_utils import get_vrm_portal_id, exit_on_error
from dbusmonitor import DbusMonitor

softwareVersion = '1.1'
logger = logging.getLogger("dbus-envoy")

# global logger for all modules imported here
#logger = logging.getLogger()

#logger.setLevel(logging.DEBUG)
logger.setLevel(logging.INFO)

driver_start_time = datetime.now()

config = None
try :
  dir_path = os.path.dirname(os.path.realpath(__file__))
  with open(dir_path + "/dbus-envoy.yaml", "r") as yamlfile:
    config = yaml.load(yamlfile, Loader=yaml.FullLoader)
  #print(config)
  if (config['Envoy']['address'] == "IP_ADDR_OF_ENVOY"):
    print("dbus-envoy.yaml file using invalid default values.")
    logger.info("dbus-envoy.yaml file using invalid default values.")
    raise

except :
  print("dbus-envoy.yaml file not found or correct.")
  logger.info("dbus-envoy.yaml file not found or correct.")
  sys.exit()

#host = os.getenv('ENVOY_HOST')
#password = os.getenv('ENVOY_PASS')

host = config['Envoy']['address']
password = config['Envoy']['password']

user = 'installer'
auth = HTTPDigestAuth(user, password)
marker = b'data: '

keep_running = True

# Have a mainloop, so we can send/receive asynchronous calls to and from dbus
DBusGMainLoop(set_as_default=True)

# callback that gets called every time a dbus value has changed
def _dbus_value_changed(dbusServiceName, dbusPath, dict, changes, deviceInstance):
  pass

# Why this dummy? Because DbusMonitor expects these values to be there, even though we don't
# need them. So just add some dummy data. This can go away when DbusMonitor is more generic.
dummy = {'code': None, 'whenToLog': 'configChange', 'accessLevel': None}
dbus_tree = {'com.victronenergy.system': 
  {'/Dc/Battery/Soc': dummy, }}

dbusmonitor = DbusMonitor(dbus_tree, valueChangedCallback=_dbus_value_changed)

# connect and register to dbus
driver = {
  'name'        : "Enphase Envoy",
  'servicename' : "enphase_envoy",
  'instance'    : 263,
  'id'          : 126,
  'version'     : 478,
}

def create_dbus_service():
  dbusservice = VeDbusService('com.victronenergy.pvinverter.envoy')
  dbusservice.add_mandatory_paths(
  processname=__file__,
  processversion=0.1,
  connection='com.victronenergy.pvinverter.envoy',
  deviceinstance=driver['instance'],
  productid=driver['id'],
  productname=driver['name'],
  firmwareversion=driver['version'],
  hardwareversion=driver['version'],
  connected=1)

  return dbusservice

dbusservice = create_dbus_service()

# /Ac/Energy/Forward     <- kWh  - Total produced energy over all phases
# /Ac/Power              <- W    - Total power of all phases, preferably real power
# /Ac/L1/Current         <- A AC
# /Ac/L1/Energy/Forward  <- kWh
# /Ac/L1/Power           <- W
# /Ac/L1/Voltage         <- V AC
# /Ac/L2/*               <- same as L1
#/StatusCode            <- 0=Startup 0; 1=Startup 1; 2=Startup 2; 3=Startup
#                          3; 4=Startup 4; 5=Startup 5; 6=Startup 6; 7=Running;
#                          8=Standby; 9=Boot loading; 10=Error

dbusservice.add_path('/Ac/Energy/Forward', value=0)
dbusservice.add_path('/Ac/Power', value=0)
dbusservice.add_path('/Ac/L1/Current', value=0)
dbusservice.add_path('/Ac/L1/Energy/Forward', value=0)
dbusservice.add_path('/Ac/L1/Power', value=0)
dbusservice.add_path('/Ac/L1/Voltage', value=0)
dbusservice.add_path('/Ac/L2/Current', value=0)
dbusservice.add_path('/Ac/L2/Energy/Forward', value=0)
dbusservice.add_path('/Ac/L2/Power', value=0)
dbusservice.add_path('/Ac/L2/Voltage', value=0)
dbusservice.add_path('/StatusCode', value=7)

# /Position     <- Input 1 = 0
#                  Output = 1
#                  Input 2 = 2
dbusservice.add_path('/Position', value=1)

stream_gauges = {
  'p': Gauge('meter_active_power_watts', 'Active Power', ['type', 'phase']),
  'q': Gauge('meter_reactive_power_watts', 'Reactive Power', ['type', 'phase']),
  's': Gauge('meter_apparent_power_watts', 'Apparent Power', ['type', 'phase']),
  'v': Gauge('meter_voltage_volts', 'Voltage', ['type', 'phase']),
  'i': Gauge('meter_current_amps', 'Current', ['type', 'phase']),
  'f': Gauge('meter_frequency_hertz', 'Frequency', ['type', 'phase']),
  'pf': Gauge('meter_power_factor_ratio', 'Power Factor', ['type', 'phase']),
}

production_gauges = {
  'activeCount': Gauge('production_active_count', 'Active Count', ['type']),
  'wNow': Gauge('power_now_watts', 'Active Count', ['type']),
  'whToday': Gauge('production_today_watthours', 'Total production today', ['type']),
  'whLastSevenDays': Gauge('production_7days_watthours', 'Total production last seven days', ['type']),
  'whLifetime': Gauge('production_lifetime_watthours', 'Total production lifetime', ['type']),
}

consumption_gauges = {
  'wNow': Gauge('consumption_now_watts', 'Active Count', ['type']),
  'whToday': Gauge('consumption_today_watthours', 'Total consumption today', ['type']),
  'whLastSevenDays': Gauge('consumption_7days_watthours', 'Total consumption last seven days', ['type']),
  'whLifetime': Gauge('consumption_lifetime_watthours', 'Total consumption lifetime', ['type']),
}

inverter_gauges = {
  'last': Gauge('inverter_last_report_watts', 'Last reported watts', ['serial', 'location']),
  'max': Gauge('inverter_max_report_watts', 'Max reported watts', ['serial', 'location']),
}


def scrape_stream():
  logger.debug("start stream thread")

  #get some data from the Victron BUS, invalid data returns NoneType
  raw_soc = dbusmonitor.get_value('com.victronenergy.system', '/Dc/Battery/Soc')
  if (raw_soc == None) :
    logger.debug("SOC is invalid")
    global keep_running
    keep_running = False
    sys.exit()

  while 1:



    #try:
      url = 'http://%s/stream/meter' % host
      stream = requests.get(url, auth=auth, stream=True, timeout=5)
      for line in stream.iter_lines():
        if (keep_running == False):
          return

        if line.startswith(marker):
          data = json.loads(line.replace(marker, b''))
          #print(data)
          for meter_type in ['production', 'net-consumption', 'total-consumption']:
            total_power = 0
            for phase in ['ph-a', 'ph-b']:
              if (meter_type == 'production'):
                #print (phase)
                #print (data.get(meter_type, {}).get(phase, {}).items())
                total_power += data.get(meter_type, {}).get(phase, {})['p']
                if (phase == 'ph-a') :
                  dbusservice['/Ac/L1/Current'] = data.get(meter_type, {}).get(phase, {})['i']
                  dbusservice['/Ac/L1/Energy/Forward'] = 10
                  dbusservice['/Ac/L1/Power'] = data.get(meter_type, {}).get(phase, {})['p']
                  dbusservice['/Ac/L1/Voltage'] = data.get(meter_type, {}).get(phase, {})['v']

                elif (phase == 'ph-b') :
                  logger.info("Total Power: {0}W".format(int(total_power)))
                  dbusservice['/Ac/Energy/Forward'] = 10
                  dbusservice["/Ac/Power"] = total_power
                  dbusservice['/StatusCode'] = 7

                  dbusservice['/Ac/L2/Current'] = data.get(meter_type, {}).get(phase, {})['i']
                  dbusservice['/Ac/L2/Energy/Forward'] = 10
                  dbusservice['/Ac/L2/Power'] = data.get(meter_type, {}).get(phase, {})['p']
                  dbusservice['/Ac/L2/Voltage'] = data.get(meter_type, {}).get(phase, {})['v']

              for key, value in data.get(meter_type, {}).get(phase, {}).items():
                if key in stream_gauges:
                  stream_gauges[key].labels(type=meter_type, phase=phase).set(value)
    # except requests.exceptions.RequestException as e:
    #   logger.debug('Exception fetching stream data: %s' % e)

    #   dbusservice['/Ac/L1/Current'] = 0
    #   dbusservice['/Ac/L1/Energy/Forward'] = 10
    #   dbusservice['/Ac/L1/Power'] = 0
    #   dbusservice['/Ac/L1/Voltage'] = 0
    #   logger.info("Total Power: 0W, offline")
    #   dbusservice['/Ac/Energy/Forward'] = 10
    #   dbusservice["/Ac/Power"] = 0
    #   dbusservice['/StatusCode'] = 10

    #   dbusservice['/Ac/L2/Current'] = 0
    #   dbusservice['/Ac/L2/Energy/Forward'] = 10
    #   dbusservice['/Ac/L2/Power'] = 0
    #   dbusservice['/Ac/L2/Voltage'] = 0

    #   time.sleep(5)


def scrape_production_json():
  url = 'http://%s/production.json' % host
  data = requests.get(url).json()
  production = data['production']
  #print(production)
  for each in production:
    mtype = each['type']
    for key in ['activeCount', 'wNow', 'whLifetime', 'whToday', 'whLastSevenDays']:
      value = each.get(key)
      if value is not None:
        production_gauges[key].labels(type=mtype).set(value)
  consumption = data['consumption']
  #print(consumption)
  for each in consumption:
    mtype = each['measurementType']
    for key in ['wNow', 'whLifetime', 'whToday', 'whLastSevenDays']:
      value = each.get(key)
      if value is not None:
        consumption_gauges[key].labels(type=mtype).set(value)


def scrape_inverters():
  url = 'http://%s/api/v1/production/inverters' % host
  data = requests.get(url, auth=auth).json()
  #print(data)
  for inverter in data:
    serial = inverter['serialNumber']
    location = config['IQ7s'].get(serial, 'unknown')
    inverter_gauges['last'].labels(serial=serial, location=location).set(inverter['lastReportWatts'])
    inverter_gauges['max'].labels(serial=serial, location=location).set(inverter['maxReportWatts'])

def scrape_handler():
  try:
    scrape_production_json()
    scrape_inverters()
  except Exception as e:
    print('Exception fetching scrape data: %s' % e)
  
  return True  # keep timer running

def exit(signal, frame):
  global keep_running
  keep_running = False

def main():
  logger.info("Driver start")

  port = config['Promethius']['port']
  logger.info("http server listening on port {0}".format(port))
  start_http_server(port)
  stream_thread = threading.Thread(target=scrape_stream)
  stream_thread.setDaemon(True)
  stream_thread.start()
  
  global _mainloop
  _mainloop = gobject.MainLoop()
  gobject.threads_init()
  context = _mainloop.get_context()

  signal.signal(signal.SIGINT, exit)

  #_mainloop = gobject.MainLoop()
  #_mainloop.run()

  # create timers (time in msec)
  scrape_handler()
  gobject.timeout_add(30000, exit_on_error, scrape_handler)

  while keep_running:
    context.iteration(True)

  logger.info("Driver stop")

if __name__ == '__main__':
  main()
