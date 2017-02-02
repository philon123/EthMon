#!/usr/bin/env python
# -*- coding: utf-8 -*-

#author Philon

import sys
import os
import signal
import threading
import subprocess
import time
import datetime
import json
import BaseHTTPServer
import urlparse
import Queue
import re

MINERMON_VERSION = "1.0.0"

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
		sys.stdout.write('config.json not found, aborting...\n')
		exit()

	if not 'miner' in newConfig: newConfig['miner'] = 'ethminer'
	if  newConfig['miner'] != 'ethminer' and newConfig['miner'] != 'optiminer':
		sys.stdout.write("Error: Invalid miner specified: {gpuApi}. Choose either amd or nvidia.\n".format(miner=newConfig['miner']))
		exit()
	if not 'pools' in newConfig: raise Exception('You need to provide pool information in the config')
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
		sys.stdout.write("Error: Invalid gpu api specified: {gpuApi}. Choose either amd or nvidia.\n".format(gpuApi=newConfig['gpuApi']))
		exit()
	if not 'gpu-memclock' in newConfig: newConfig['gpu-memclock'] = 0
	if not 'gpu-vddc' in newConfig: newConfig['gpu-vddc'] = 0
	if not 'ethminer-params' in newConfig: newConfig['ethminer-params'] = {}

	return newConfig

def getSystemUptime():
	with open('/proc/uptime', 'r') as f:
		return int(float(f.readline().split()[0]))

def isProgramRunning(name):
	if name == '': return False
	out = subprocess.Popen('pidof {name}'.format(name=name), stdout=subprocess.PIPE, shell=True).communicate()[0]
	return out != ''
	#for t in out.split('\n'):
	#	if len(t)>0 and not 'grep' in t:
	#		return True
	#return False

def castFloat(x):
	try:
		return float(x)
	except ValueError as e:
		return -1.0

''' Starts/Stops the mining process and other services '''
class MinerMon(object):
	def __init__(self):
		global config
		self.killer = GracefulKiller()
		self.minerProcess = None
		self.minerProcessName = ''
		self.server = MinerMonServer()
		self.server.start(8042)
		self.outputReader = None
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
		if self.minerProcessName != '':
			os.system('killall {process} 2>/dev/null'.format(process=self.minerProcessName))
		time.sleep(1)
		if isProgramRunning(self.minerProcessName):
			sys.stdout.write("detected running, hanging miner. rebooting in 5s...\n")
			time.sleep(5)
			os.system('systemctl reboot -f')

		if config['miner'] == 'ethminer':
			self.minerProcess = subprocess.Popen(
				'ethminer -G -F {pool} {ethminerParams}'.format(
					pool = config['pools'][0]['url'],
					ethminerParams = ' '.join([
						'--{k} {v}'.format(
							k=k,
							v='"' + v + '"' if type(v)==str else v
						)
						for k,v in config['ethminer-params'].iteritems()
					])
				),
				stderr=subprocess.PIPE,
				shell=True
			)
			self.minerProcessName = 'ethminer'
			self.outputReader = EthminerOutputReader()
			self.outputReader.start(self.minerProcess.stderr)
		elif config['miner'] == 'optiminer':
			self.minerProcess = subprocess.Popen(
				'./optiminer-zcash -s "{url}" -u "{user}" -p "{password}"'.format(
					url = config['pools'][0]['url'],
					user = config['pools'][0]['user'],
					password = config['pools'][0]['pass']
				),
				cwd = '/opt/optiminer-zcash',
				stdout=subprocess.PIPE,
				shell=True
			)
			self.minerProcessName = 'optiminer-zcash'
			self.outputReader = OptiminerOutputReader()
			self.outputReader.start(self.minerProcess.stdout)


	def stop(self):
		self.gpuApi.resetClocks()
		self.minerProcess.kill()
		self.server.stop()
		self.outputReader.stop()

	def mainLoop(self):
		crashTestTimer = 0
		hangTestTimer = 0
		updateTimer = 3
		autotuneTimer = 0

		while not self.killer.kill_now:
			#check for miner crash
			crashTestTimer += 1
			if crashTestTimer >= 10:
				crashTestTimer = 0
				if not isProgramRunning(self.minerProcessName):
					sys.stdout.write("detected miner crash. exiting in 5s...\n")
					time.sleep(5)
					raise Exception('detected miner crash')

			#check for miner hang
			hangTestTimer += 1
			if hangTestTimer >= 60:
				hangTestTimer = 0
				if self.outputReader.getSecsSinceLastOutput() >= 60*20:
					sys.stdout.write("detected miner hanging. rebooting...\n")
					time.sleep(2)
					os.system('systemctl reboot -f')

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
					if oldFanPercent != newFanPercent or oldCoreClockPercent != newCoreClockPercent:
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
		cardsMhs = self.outputReader.getCardsMhs()
		if len(new_card_data) == 0 and len(cardsMhs) > 0:
			card = dict()
			card['adapter_nr'] = 0
			card['name'] = 'DUMMY CARD'
			card['mhs'] = sum([c['mhs'] for c in cardsMhs])
			card['temperature'] = 0
			card['fan_percent'] = 0
			card['curr_core_clock'] = 0
			card['curr_mem_clock'] = 0
			new_card_data = {"0": card}
		else:
			for i, card in new_card_data.iteritems():
				card['name'] = str(card['adapter_nr']) + ' - ' + card['description']
				card['mhs'] = cardsMhs[str(card['adapter_nr'])] if str(card['adapter_nr']) in cardsMhs else 0.0
		global card_data
		card_data = new_card_data

'''Starts API in a new thread'''
class MinerMonServer(object):
	def __init__(self):
		self.server = None
		self.serverThread = None

	def start(self, port):
		self.server = BaseHTTPServer.HTTPServer(('', port), MinerMonRequestHandler)
		self.serverThread = threading.Thread(target=self.server_thread)
		self.serverThread.daemon = True
		self.serverThread.start()

	def stop(self):
		self.server.shutdown()
		self.serverThread.join()
		sys.stdout.write('Stopped MinerMon server\n')

	def server_thread(self):
		sys.stdout.write('Started MinerMon server\n')
		self.server.serve_forever()

''' Handle API requests. '''
class MinerMonRequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):
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
			new_rig_object['pools'] = config['pools']
			new_rig_object['elapsed_secs'] = round(time.time() - start_time)
			new_rig_object['firmwareVersion'] = MINERMON_VERSION
			new_rig_object['miner'] = config['miner']
			new_rig_object['miners'] = []
			for i, card in card_data.iteritems():
				newCard = card.copy()
				del newCard['description']
				del newCard['adapter_nr']
				del newCard['voltage']
				del newCard['peak_core_clock']
				del newCard['peak_mem_clock']
				new_rig_object['miners'].append(newCard)
			new_rig_object['miners'].sort(key=lambda card: card['name'])

			result = new_rig_object
		elif cmd == 'getconfig':
			result = config
		else:
			result['result'] = 'Usage: "getdata" to get data, "getconfig" to get config. '

		self.wfile.write(json.dumps(result, indent=2))

		if not (cmd == 'getdata' and self.client_address[0] == '127.0.0.1'):
			sys.stdout.write("{timestamp} MinerMon just answered a {cmd} request from {client}\n".format(
				timestamp = datetime.datetime.now().strftime('%H:%M:%S'),
				cmd = cmd,
				client = self.client_address[0]
			))

'''Template for different output readers'''
'''continuously reads data from a processes stdout'''
class MinerOutputReader(object):
	def __init__(self):
		self.should_stop = False
		self.sr = None
		self.t = None

		self.mhsCache = {}
		self.cardsMhs = {}
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
			time.sleep(0.1)
			newOut = self.sr.readlastline()
			if not newOut == '':
				self.lastOutputTime = int(time.time())
				self.readOutputLine(newOut)

	def readOutputLine(self, newLine):
		raise Exception('not implemented!')

	def addNewMhsValue(self, cardNr, newMhs):
		#ensure cache for this card exists
		if not str(cardNr) in self.mhsCache: self.mhsCache[str(cardNr)] = []
		#add new value
		self.mhsCache[str(cardNr)].insert(0, newMhs)
		#ensure only the newest values are in the cache
		del self.mhsCache[str(cardNr)][10:]
		#update current card mhs value from cache
		self.cardsMhs[str(cardNr)] = sum(self.mhsCache[str(cardNr)], 0.0) / len(self.mhsCache[str(cardNr)])
		#remember time of latest update
		self.lastMhsTime = int(time.time())

	def getCardsMhs(self):
		if int(time.time()) - self.lastMhsTime > 30:
			self.cardsMhs = {}
		return self.cardsMhs

	def stop(self):
		self.should_stop = True
		self.t.join()

	def getSecsSinceLastOutput(self):
		if self.lastOutputTime == 0: return 0
		return int(time.time()) - self.lastOutputTime

class EthminerOutputReader(MinerOutputReader):
	def readOutputLine(self, newLine):
		if not 'Mining on PoWhash' in newLine: return
		parts = newLine.split(' ')
		for i, p in enumerate(parts):
			if p == 'H/s':
				mhs = float(parts[i-1]) / 1000000.0 / 6.0
				self.addNewMhsValue(0, mhs)
				self.addNewMhsValue(1, mhs)
				self.addNewMhsValue(2, mhs)
				self.addNewMhsValue(3, mhs)
				self.addNewMhsValue(4, mhs)
				self.addNewMhsValue(5, mhs)
				# miner  23:44:28|ethminer  Mining on PoWhash #e957e159?? : 26503849 H/s = 199229440 hashes / 7.517 s

class OptiminerOutputReader(MinerOutputReader):
	def readOutputLine(self, newLine):
		if not ('GPU' in newLine and 'S/s' in newLine): return
		#2016-11-16 18:52:59,943 INFO  [GPU0]  49.0 I/s 110.0 S/s (1s) 49.1 I/s 91.3 S/s (1m)
		data = re.findall(r'GPU(\d).*I/s ([\d\.]+) S/s \(1s\)', newLine)
		cardNr = int(data[0][0])
		cardMhs = float(data[0][1])/1000000
		self.addNewMhsValue(cardNr, cardMhs)

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
			sys.stdout.write(line)
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
	def resetClocks(self):
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
		isSaved = os.path.isfile('/run/minermon/clock')
		if not isSaved:
			cardData = self.getAmdconfigData()
			newDefaultCoreClocks = [card['peak_core_clock'] for i,card in cardData.iteritems()]
			if len(newDefaultCoreClocks) == 0: raise Exception('Error: No cards found. ')
			newDefaultCoreClock = None
			for c in newDefaultCoreClocks:
				if newDefaultCoreClock is None: newDefaultCoreClock = c
				if newDefaultCoreClock != c: raise Exception('Error: Different card types detected. ')
			if not os.path.exists('/run/minermon'):
				os.makedirs('/run/minermon')
			with open('/run/minermon/clock', 'wb') as clockFile:
				clockFile.write(str(newDefaultCoreClock))
		with open('/run/minermon/clock', 'rb') as clockFile:
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
	def resetClocks(self):
		sys.stdout.write('Resetting clocks to default...\n')
		os.system('atitweak -d')
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
			os.system('systemctl reboot -f')
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
	def resetClocks(self):
		pass
	def updateCardData(self):
		return {}

class GracefulKiller:
	kill_now = False
	def __init__(self):
		signal.signal(signal.SIGINT, self.exit_gracefully)
		signal.signal(signal.SIGTERM, self.exit_gracefully)
	def exit_gracefully(self,signum, frame):
		self.kill_now = True

if __name__ == '__main__':
	os.environ['DISPLAY'] = ':0'
	os.environ['GPU_SINGLE_ALLOC_PERCENT'] = '100'
	os.environ['GPU_MAX_ALLOC_PERCENT'] = '100'

	config = loadConfig()
	minermon = MinerMon()
	minermon.start()
	try:
		minermon.mainLoop()
	finally:
		minermon.stop()
