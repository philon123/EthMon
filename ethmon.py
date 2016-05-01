#!/usr/bin/env python
# -*- coding: utf-8 -*-

#author Philon

import sys
import os
import threading
import subprocess
import time
import datetime
import json
import BaseHTTPServer
import urlparse
import Queue
import re

ETHMON_VERSION = "0.7.5"
#FGLRX_VERSION = "UNKNOWN"
#try:
#	FGLRX_VERSION = subprocess.Popen(
#		'dmesg | grep "\[fglrx\] module loaded" | grep -oE "[0-9]+\.[0-9]+\.[0-9]*"',
#		stdout=subprocess.PIPE,
#		shell=True
#	).communicate()[0].strip()
#except Exception:
#	sys.stdout.write("unable to find fglrx version\n")

config = dict()
card_data = dict()

start_time = 0

def loadConfig():
	if len(sys.argv) == 1:
		sys.stdout.write('must specify a config as parameter, aborting...\n')
		exit()

	configUrl = sys.argv[1]
	try:
		with open(configUrl) as confFile:
			try:
				newConfig = json.loads(confFile.read())
			except ValueError:
				sys.stdout.write('config is invalid json, aborting...\n')
				exit()
	except IOError:
		sys.stdout.write('ethminer.conf not found, aborting...\n')
		exit()

	if not 'minFanPercent' in newConfig: newConfig['minFanPercent'] = 50
	if not 'maxFanPercent' in newConfig: newConfig['maxFanPercent'] = 85
	if not 'minFanAtTemp' in newConfig: newConfig['minFanAtTemp'] = 50
	if not 'maxFanAtTemp' in newConfig: newConfig['maxFanAtTemp'] = 85
	if not 'maxTemp' in newConfig: newConfig['maxTemp'] = 90
	if not 'minCoreClockPercent' in newConfig: newConfig['minCoreClockPercent'] = 30
	if not 'maxCoreClockPercent' in newConfig: newConfig['maxCoreClockPercent'] = 100
	if not 'coreClockPercentStep' in newConfig: newConfig['coreClockPercentStep'] = 5
	if not 'gpuApi' in newConfig: newConfig['gpuApi'] = 'amd'
	if  newConfig['gpuApi'] != 'amd' and newConfig['gpuApi'] != 'nvidia':
		sys.stdout.write("Error: Invalid gpu api specified: {gpuApi}. Choose either amd or nvidia.\n".format(gpuApi=gpuApi))
		exit()
	if not 'gpu-memclock' in newConfig: newConfig['gpu-memclock'] = 0
	if not 'gpu-vddc' in newConfig: newConfig['gpu-vddc'] = 0
	if not 'farm-recheck' in newConfig: newConfig['farm-recheck'] = 0

	return newConfig

def getSystemUptime():
	with open('/proc/uptime', 'r') as f:
		return int(float(f.readline().split()[0]))

def isProgramRunning(name):
	out = subprocess.Popen('ps aux | grep {name}'.format(name=name), stdout=subprocess.PIPE, shell=True).communicate()[0]
	for t in out.split('\n'):
		if len(t)>0 and not 'grep' in t:
			return True
	return False

def castFloat(x):
	try:
		return float(x)
	except ValueError as e:
		return -1.0

''' Starts/Stops the mining process and other services '''
class Ethmon(object):
	def __init__(self):
		self.minerProcess = None
		self.server = EthmonServer()
		self.server.start(8042)
		self.outputReader = EthminerOutputReader()
		global config
		if config['gpuApi'] == 'amd':
			sys.stdout.write("using amd api\n")
			self.gpuApi = AmdApi()
		elif config['gpuApi'] == 'nvidia':
			sys.stdout.write("using nvidia api\n")
			self.gpuApi = NvidiaApi()

		global start_time
		start_time = time.time()

		config['defaultCoreClock'] = self.gpuApi.getDefaultClock()
		sys.stdout.write("using this config: \n" + json.dumps(config, indent=4) + "\n")

		sys.stdout.write("setting core clocks to: " + str(config['defaultCoreClock'] * config['maxCoreClockPercent'] / 100.0) + "\n")
		self.gpuApi.setClocks(config['defaultCoreClock'] * config['maxCoreClockPercent'] / 100.0, config['gpu-memclock'])

	def start(self):
		os.system('killall ethminer 2>/dev/null')
		time.sleep(1)
		if isProgramRunning('ethminer'):
			sys.stdout.write("detected running, hanging ethminer. rebooting in 5s...\n")
			time.sleep(5)
			os.system('reboot')

		self.minerProcess = subprocess.Popen(
			'ethminer -G -F {pool} {recheck}'.format(
				pool = config['poolUrl'],
				recheck = '--farm-recheck ' + str(config['farm-recheck']) if config['farm-recheck']!=0 else ''
			),
			stderr=subprocess.PIPE,
			shell=True
		)
		self.outputReader.start(self.minerProcess.stderr)

	def stop(self):
		self.outputReader.stop()
		self.minerProcess.kill()

	def restart(self):
		self.stop()
		self.start()

	def mainLoop(self):
		crashTestTimer = 0
		hangTestTimer = 0
		updateTimer = 3
		autotuneTimer = 0
		while True:
			#check for ethminer crash
			crashTestTimer += 1
			if crashTestTimer >= 10:
				crashTestTimer = 0
				if not isProgramRunning('ethminer'):
					sys.stdout.write("detected ethminer crash. restarting miner process...\n")
					self.restart()

			#check for ethminer hang
			hangTestTimer += 1
			if hangTestTimer >= 60:
				hangTestTimer = 0
				if self.outputReader.getSecsSinceLastOutput() >= 60*20:
					sys.stdout.write("detected ethminer hanging. rebooting...\n")
					time.sleep(2)
					os.system('reboot')

			#update card_data
			updateTimer += 1
			if updateTimer >= 5:
				self.updateRigObject()
				updateTimer = 0

			#autotune
			autotuneTimer += 1
			if autotuneTimer >= 30:
				autotuneTimer = 0
				self.autotune()

			time.sleep(1)

	def autotune(self):
		for i,card in card_data.iteritems():
			if 'DUMMY CARD' in card['description']:
				sys.stdout.write("No card was found. For safety, setting all fans to " + str(config['maxFanPercent']) + "%\n")
				self.gpuApi.setFanSpeeds(config['maxFanPercent'])
			else:
				oldFanPercent = card['fan_percent']
				oldCoreClock = card['peak_core_clock']
				oldCoreClockPercent = round(100.0 * card['peak_core_clock'] / config['defaultCoreClock'])
				newFanPercent = config['maxFanPercent']
				newCoreClockPercent = oldCoreClockPercent
				if card['temperature'] == -1.0:
					newFanPercent = config['maxFanPercent']
					newCoreClockPercent = oldCoreClockPercent
					sys.stdout.write("Temp of adapter {nr} is UNKNOWN, setting fan speed {fan}%, core clock {core}%\n".format(
						nr = card['adapter_nr'],
						fan = config['maxFanPercent'],
						core = config['minCoreClockPercent']
					))
				else:
					tempFractionOfMax = (card['temperature'] - config['minFanAtTemp']) / (config['maxFanAtTemp'] - config['minFanAtTemp'])
					tempFractionOfMax = min(max(0, tempFractionOfMax), 1)
					newFanPercent = int(tempFractionOfMax * (config['maxFanPercent'] - config['minFanPercent']) + config['minFanPercent'])
					if card['temperature'] > config['maxTemp']:
						newCoreClockPercent -= config['coreClockPercentStep']
					elif card['temperature'] < config['maxTemp']:
						newCoreClockPercent += config['coreClockPercentStep']
					newCoreClockPercent = min(max(newCoreClockPercent, config['minCoreClockPercent']), config['maxCoreClockPercent'])
					newCoreClock = round((newCoreClockPercent/100.0) * config['defaultCoreClock'])
					sys.stdout.write("Temp of adapter {nr} is {temp}, setting fan speed {oldFan}% -> {fan}%, core clock {oldCorePercent}% -> {corePercent}% ({oldCore} MHz -> {core}MHz)\n".format(
						nr = card['adapter_nr'],
						temp = card['temperature'],
						oldFan = oldFanPercent,
						fan = newFanPercent,
						oldCorePercent = oldCoreClockPercent,
						corePercent = newCoreClockPercent,
						oldCore = oldCoreClock,
						core = newCoreClock
					))
				self.gpuApi.setFanSpeed(card['adapter_nr'], newFanPercent)
				self.gpuApi.setClock(card['adapter_nr'], newCoreClock, config['gpu-memclock'])

	def updateRigObject(self):
		new_card_data = self.gpuApi.getCardData()
		totalMhs = self.outputReader.getMhs()
		if len(new_card_data) == 0 and totalMhs > 0:
			card = dict()
			card['adapter_nr'] = 0
			card['description'] = 'DUMMY CARD'
			card['temperature'] = 0
			card['fan_percent'] = 0
			card['curr_core_clock'] = 0
			#card['peak_core_clock'] = 0
			card['curr_mem_clock'] = 0
			#card['peak_mem_clock'] = 0
			#card['voltage'] = 0
			new_card_data = {"0": card}
		mhsPerCard = round(totalMhs / (len(new_card_data) if len(new_card_data)>0 else 1), 2)
		for i, card in new_card_data.iteritems():
			card['name'] = str(card['adapter_nr']) + ' - ' + card['description']
			card['mhs'] = mhsPerCard
		global card_data
		card_data = new_card_data

'''Starts API in a new thread'''
class EthmonServer(object):
	def __init__(self):
		self.server = None
		self.serverThread = None

	def start(self, port):
		self.server = BaseHTTPServer.HTTPServer(('', port), EthmonRequestHandler)
		self.serverThread = threading.Thread(target=self.server_thread)
		self.serverThread.daemon = True
		self.serverThread.start()

	def stop(self):
		self.server.shutdown()
		self.serverThread.join()
		sys.stdout.write('Stopped EthMon server\n')

	def server_thread(self):
		sys.stdout.write('Started EthMon server\n')
		self.server.serve_forever()

''' Handle API requests. '''
class EthmonRequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):
	def log_message(self, format, *args):
		return
	def do_HEAD(self):
		self.send_response(200)
		self.send_header("Content-type", "text/html")
		self.end_headers()

	def do_GET(self):
		self.send_response(200)
		self.send_header("Content-type", "text/html")
		self.end_headers()

		result = dict()
		query_components = urlparse.parse_qs(urlparse.urlparse(self.path).query)
		if not 'cmd' in query_components:
			result['error'] = 'You need to specify a command via &cmd=mycommand'
		cmd = query_components['cmd'][0]
		if cmd == 'getdata':
			new_rig_object = dict()
			new_rig_object['poolUrl'] = config['poolUrl']
			new_rig_object['elapsed_secs'] = round(time.time() - start_time)
			new_rig_object['firmwareVersion'] = ETHMON_VERSION# + ", fglrx: " + FGLRX_VERSION
			new_rig_object['miners'] = []
			for i, card in card_data.iteritems():
				newCard = card.copy()
				del newCard['description']
				del newCard['adapter_nr']
				del newCard['voltage']
				del newCard['peak_core_clock']
				del newCard['peak_mem_clock']
				new_rig_object['miners'].append(newCard)

			result = new_rig_object
		elif cmd == 'getconfig':
			result = config
		else:
			result['result'] = 'Usage: "getdata" to get data. '

		self.wfile.write(json.dumps(result, indent=2))

		sys.stdout.write("{timestamp} EthMon just answered a {cmd} request from {client}\n".format(
			timestamp = datetime.datetime.now().strftime('%H:%M:%S'),
			cmd = cmd,
			client = self.client_address[0]
		))

'''Starts ethminer process and continuously reads data from its stdout'''
class EthminerOutputReader(object):
	def __init__(self):
		self.should_stop = False
		self.sr = None
		self.t = None

		self.mhs = 0
		self.lastMhsTime = 0
		self.lastOutputTime = 0

	def start(self, outputStream):
		self.should_stop = False
		self.sr = NonBlockingStreamReader(outputStream)
		self.t = threading.Thread(target=self.parseStream)
		self.t.daemon = True
		self.t.start()

	def parseStream(self):
		while not self.should_stop:
			time.sleep(0.5)
			newOut = self.sr.readlastline()
			if not newOut == '':
				self.lastOutputTime = int(time.time())
			if not 'Mining on PoWhash' in newOut:
				continue
			parts = newOut.split(' ')
			for i, p in enumerate(parts):
				if p == 'H/s':
					mhs = float(parts[i-1]) / 1000000
					self.mhs = mhs
					self.lastMhsTime = int(time.time())
			# miner  23:44:28|ethminer  Mining on PoWhash #e957e159?? : 26503849 H/s = 199229440 hashes / 7.517 s

	def getMhs(self):
		if int(time.time()) - self.lastMhsTime > 30:
			self.mhs = 0
		return self.mhs

	def stop(self):
		self.should_stop = True
		self.t.join()

	def getSecsSinceLastOutput(self):
		if self.lastOutputTime == 0: return 0
		return int(time.time()) - self.lastOutputTime

''' Allows to read lines from a stream while read operation is blocking'''
class NonBlockingStreamReader:
	def __init__(self, stream):
		self._s = stream
		self._q = Queue.Queue()

		def _populateQueue(_stream, queue):
			while True:
				line = _stream.readline()
				if line:
					queue.put(line)
				time.sleep(0.01)

		self._t = threading.Thread(target=_populateQueue, args=(self._s, self._q))
		self._t.daemon = True
		self._t.start()

	def readline(self, timeout=None):
		try:
			line = self._q.get(block=timeout is not None, timeout=timeout)
			sys.stdout.write(line + '\n')
			return line
		except Queue.Empty:
			return None

	def readlastline(self):
		result = ''
		while True:
			tmp = self.readline()
			if tmp is None: break
			result = tmp
		return result

''' Interface for communication with GPU Api '''
class GpuApi(object):
	def __init__(self):
		self.cardData = dict()
		self.cardDataThread = threading.Thread(target=self.updateCardData)
		self.cardDataThread.daemon = True
		self.cardDataThread.start()
	def getDefaultClocks(self):
		raise Exception("not implemented")
	def getCardData(self):
		return self.cardData
	def setFanSpeed(self, adapterIndex, newPercent):
		raise Exception("not implemented")
	def setFanSpeeds(self, newPercent):
		raise Exception("not implemented")
	def setClock(self, adapterIndex, newCoreClock, newMemClock):
		raise Exception("not implemented")
	def setClocks(self, newCoreClock, newMemClock):
		raise Exception("not implemented")
	def updateCardData(self):
		raise Exception("not implemented")

def execCommandInThread(cmd):
	t = threading.Thread(target=os.system, args=(cmd,))
	t.daemon = True
	t.start()

''' Requires adl3 in /opt/scripts/adl3/ '''
class AmdApi(GpuApi):
	def __init__(self):
		super(AmdApi, self).__init__()
		os.system('amdconfig --od-enable')
		if config['gpu-vddc'] != 0:
			os.system('atitweak -v {voltage}'.format(voltage = config['gpu-vddc']))
	def getDefaultClock(self):
		isSaved = os.path.isfile('/run/ethmon/clock')
		if not isSaved:
			cardData = self.getAmdconfigData()
			newDefaultCoreClocks = [card['peak_core_clock'] for i,card in cardData.iteritems()]
			if len(newDefaultCoreClocks) == 0: raise Exception('Error: No cards found. ')
			newDefaultCoreClock = None
			for c in newDefaultCoreClocks:
				if newDefaultCoreClock is None: newDefaultCoreClock = c
				if newDefaultCoreClock != c: raise Exception('Error: Different card types detected. ')
			if not os.path.exists('/run/ethmon'):
				os.makedirs('/run/ethmon')
			with open('/run/ethmon/clock', 'wb') as clockFile:
				clockFile.write(str(newDefaultCoreClock))
		with open('/run/ethmon/clock', 'rb') as clockFile:
			return float(clockFile.read())
	def setFanSpeed(self, adapterIndex, newPercent):
		execCommandInThread('/opt/scripts/adl3/atitweak -A {adapterIndex} -f {percent} >/dev/null'.format(
			percent=int(newPercent),
			adapterIndex=int(adapterIndex)
		))
	def setFanSpeeds(self, newPercent):
		execCommandInThread('/opt/scripts/adl3/atitweak -f {percent} >/dev/null'.format(percent=int(newPercent)))
	def setClock(self, adapterIndex, newCoreClock, newMemClock):
		execCommandInThread('amdconfig --adapter={adapterIndex} --od-setclocks={clock},{memClock} >/dev/null'.format(
			adapterIndex=int(adapterIndex),
			clock=int(newCoreClock),
			memClock=int(newMemClock)
		))
	def setClocks(self, newCoreClock, newMemClock):
		execCommandInThread('amdconfig --adapter=all --od-setclocks={coreClock},{memClock} >/dev/null'.format(
			coreClock=int(newCoreClock),
			memClock=int(newMemClock)
		))
	def updateCardData(self):
		while True:
			startTime = time.time()
			try:
				atitweakData = self.getAtitweakData()
				amdconfigData = self.getAmdconfigData()
				if len(atitweakData) != len(amdconfigData):
					raise ValueError("Atitweak found {at} cards, amdconfig found {ac}"
						.format(
							at = len(atitweakData),
							ac = len(amdconfigData)
						)
					)

				newCardData = atitweakData
				for i, data in amdconfigData.iteritems():
					newCardData[i].update(data)
				#sys.stdout.write(json.dumps(newCardData, indent=4) + "\n")
				self.cardData = newCardData
			except Exception as e:
				sys.stdout.write("Error while updating card data: " + str(e) + "\n")

			sleepTime = 10 - min(10, time.time()-startTime)
			time.sleep(sleepTime)
	def getAtitweakData(self):
		o,e = subprocess.Popen('/opt/scripts/adl3/atitweak -s', stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True).communicate()
		if "Segmentation fault" in e:
			sys.stdout.write("adl crash detected, rebooting...\n")
			time.sleep(2)
			os.system('reboot')
		adapters = zip(*re.findall(r'(\d)\.(.+)\(.*\)', o))
		if len(adapters)==0: raise Exception("No adapters found: " + o)
		adapterNrs = map(castFloat, list(adapters[0]))
		descriptions = map(str.strip, list(adapters[1]))
		temps = map(castFloat, re.findall(r'temperature ([0-9eE\+\-\.]*) C', o))
		fans = zip(*re.findall(r'(fan speed ([0-9eE\+\-\.]*)%)|(fan speed [0-9]* RPM)|(unable to get fan speed)', o))
		if fans==[]: raise Exception("No fan speeds found: " + o)
		fans = map(castFloat, list(fans[1]))
		clocks = map(castFloat, re.findall(r'engine clock ([0-9eE\+\-\.]*)MHz', o))
		voltages = map(castFloat, re.findall(r'core voltage ([0-9eE\+\-\.]*)VDC', o))

		newCardData = dict()
		for i in range(len(adapterNrs)):
			card = dict()
			card['adapter_nr'] = int(adapterNrs[i])
			card['description'] = descriptions[i]
			card['temperature'] = temps[i]
			card['fan_percent'] = fans[i]
			#card['clock'] = clocks[i]
			card['voltage'] = voltages[i]
			newCardData[str(card['adapter_nr'])] = card
		return newCardData
	def getAmdconfigData(self):
		o,e = subprocess.Popen('amdconfig --adapter=all --od-getclocks', stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True).communicate()
		newo = ""
		for line in o.splitlines():
			if not "ERROR" in line: newo += line + "\n"
		o = newo
		adapters = zip(*re.findall(r'Adapter (\d) - (.+)$', o, flags=re.MULTILINE))
		if len(adapters)==0: raise Exception("No adapters found: " + o)
		adapterNrs = map(castFloat, list(adapters[0]))
		descriptions = map(str.strip, list(adapters[1]))

		current = zip(*re.findall(r'Current Clocks :\W+(\d+)\W+(\d+)', o))
		currCores = map(castFloat, list(current[0]))
		currMems = map(castFloat, list(current[1]))

		peaks =  zip(*re.findall(r'Current Peak :\W+(\d+)\W+(\d+)', o))
		peakCores = map(castFloat, list(peaks[0]))
		peakMems = map(castFloat, list(peaks[1]))

		newCardData = dict()
		for i in range(len(adapterNrs)):
			card = dict()
			card['adapter_nr'] = int(adapterNrs[i])
			card['description'] = descriptions[i]
			card['curr_core_clock'] = currCores[i]
			card['curr_mem_clock'] = currMems[i]
			card['peak_core_clock'] = peakCores[i]
			card['peak_mem_clock'] = peakMems[i]
			newCardData[str(card['adapter_nr'])] = card
		#sys.stdout.write(json.dumps(newCardData) + "\n")
		return newCardData

#dummy for now. allows to monitor hashrates only
class NvidiaApi(GpuApi):
	def getDefaultClocks(self):
		return {}
	def setFanSpeed(self, adapterIndex, newPercent):
		return
	def setFanSpeeds(self, newPercent):
		return
	def setClock(self, adapterIndex, newCoreClock, newMemClock):
		return
	def setClocks(self, newCoreClock, newMemClock):
		return
	def updateCardData(self):
		return {}

if __name__ == '__main__':
	os.environ['DISPLAY'] = ':0'
	os.environ['GPU_SINGLE_ALLOC_PERCENT'] = '100'
	os.environ['GPU_MAX_ALLOC_PERCENT'] = '100'

	config = loadConfig()
	ethmon = Ethmon()
	ethmon.start()
	try:
		ethmon.mainLoop()
	finally:
		ethmon.stop()
