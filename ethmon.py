#!/usr/bin/env python
# -*- coding: utf-8 -*-

#author Philon

import sys
import os
import threading
import subprocess
import time
import json
import BaseHTTPServer
import urlparse
import Queue
import re

ETHMON_VERSION = "0.7.2" #use semantic versioning :) www.semver.org
FGLRX_VERSION = "UNKNOWN"
try:
	FGLRX_VERSION = subprocess.Popen(
		'dmesg | grep "\[fglrx\] module loaded" | grep -oE "[0-9]+\.[0-9]+\.[0-9]*"', 
		stdout=subprocess.PIPE, 
		shell=True
	).communicate()[0].strip()
except Exception:
	print "unable to find fglrx version"
	
config = dict()
card_data = dict()

def loadConfig():
	if len(sys.argv) == 1:
		print 'must specify a config as parameter, aborting...'
		exit()

	configUrl = sys.argv[1]
	try:
		with open(configUrl) as confFile:
			try:
				newConfig = json.loads(confFile.read())
			except ValueError:
				print 'config is invalid json, aborting...'
				exit()
	except IOError:
		print 'ethminer.conf not found, aborting...'
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
		print "Error: Invalid gpu api specified: {gpuApi}. Choose either amd or nvidia.".format(gpuApi=gpuApi)
		exit()
		
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
			print "using amd api"
			self.gpuApi = AmdApi()
		elif config['gpuApi'] == 'nvidia':
			print "using nvidia api"
			self.gpuApi = NvidiaApi()
		
		config['defaultCoreClocks'] = self.gpuApi.getDefaultClocks()
		if len(config['defaultCoreClocks']) > 0:
			self.gpuApi.setCoreClocks(config['defaultCoreClocks'][0])
		self.startTime = time.time()
		
	def start(self):
		print "using this config: \n" + json.dumps(config, indent=4)
		
		os.system('killall ethminer 2>/dev/null')
		time.sleep(1)
		if isProgramRunning('ethminer'):
			print "detected running, hanging ethminer. rebooting in 5s..."
			time.sleep(5)
			os.system('reboot')
			
		self.minerProcess = subprocess.Popen(
			'ethminer -G -F ' + config['poolUrl'],
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
		fanTimer = 29
		autotuneTimer = 25
		while True:
			#check for ethminer crash
			crashTestTimer += 1
			if crashTestTimer >= 10:
				crashTestTimer = 0
				if not isProgramRunning('ethminer'):
					print "detected ethminer crash. restarting miner process..."
					self.restart()
					
			#check for ethminer hang
			hangTestTimer += 1
			if hangTestTimer >= 60:
				hangTestTimer = 0
				if self.outputReader.getSecsSinceLastOutput() >= 60*20:
					print "detected ethminer hanging. rebooting..."
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
				print "No card was found. For safety, setting all fans to " + str(config['maxFanPercent']) + "%"
				self.gpuApi.setFanSpeeds(config['maxFanPercent'])
			else:
				oldFanPercent = card['fan_percent']
				oldCoreClock = card['curr_core_clock']
				oldCoreClockPercent = round(100.0 * card['curr_core_clock'] / config['defaultCoreClocks'][i])
				newFanPercent = config['maxFanPercent']
				newCoreClockPercent = oldCoreClockPercent
				if card['temperature'] == -1.0:
					newFanPercent = config['maxFanPercent']
					newCoreClockPercent = oldCoreClockPercent
					print "Temp of adapter {nr} is UNKNOWN, setting fan speed {fan}%, core clock {core}%".format(
						nr = card['adapter_nr'],
						fan = config['maxFanPercent'],
						core = config['minCoreClockPercent']
					)
				else:
					tempFractionOfMax = (card['temperature'] - config['minFanAtTemp']) / (config['maxFanAtTemp'] - config['minFanAtTemp'])
					tempFractionOfMax = min(max(0, tempFractionOfMax), 1)
					newFanPercent = int(tempFractionOfMax * (config['maxFanPercent'] - config['minFanPercent']) + config['minFanPercent'])
					if card['temperature'] > config['maxTemp']:
						newCoreClockPercent -= config['coreClockPercentStep']
					elif card['temperature'] < config['maxTemp']:
						newCoreClockPercent += config['coreClockPercentStep']
					newCoreClockPercent = min(max(newCoreClockPercent, config['minCoreClockPercent']), config['maxCoreClockPercent'])
					newCoreClock = round((newCoreClockPercent/100.0) * config['defaultCoreClocks'][i])
					print "Temp of adapter {nr} is {temp}, setting fan speed {oldFan}% -> {fan}%, core clock {oldCorePercent}% -> {corePercent}% ({oldCore} MHz -> {core}MHz)".format(
						nr = card['adapter_nr'],
						temp = card['temperature'],
						oldFan = oldFanPercent,
						fan = newFanPercent,
						oldCorePercent = oldCoreClockPercent,
						corePercent = newCoreClockPercent,
						oldCore = oldCoreClock,
						core = newCoreClock
					)
				self.gpuApi.setFanSpeed(card['adapter_nr'], newFanPercent)
				self.gpuApi.setCoreClock(card['adapter_nr'], newCoreClock)
				
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
			card['peak_core_clock'] = 0
			card['curr_mem_clock'] = 0
			card['peak_mem_clock'] = 0
			card['voltage'] = 0
			new_card_data = {"0": card}
		mhsPerCard = round(totalMhs / (len(new_card_data) if len(new_card_data)>0 else 1), 2)
		for i, card in new_card_data.iteritems():
			card['name'] = str(card['adapter_nr']) + ' - ' + card['description']
			card['mhs'] = mhsPerCard
			card['elapsedSecs'] = round(time.time() - self.startTime) #TODO delete later
			card['elapsed_secs'] = card['elapsedSecs']
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
		print 'Stopped EthMon server'

	def server_thread(self):
		print 'Started EthMon server'
		self.server.serve_forever()

''' Handle API requests. '''
class EthmonRequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):
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
			new_rig_object['firmwareVersion'] = ETHMON_VERSION + ", fglrx: " + FGLRX_VERSION
			new_rig_object['miners'] = []
			for i, card in card_data.iteritems():
				new_rig_object['miners'].append(card.copy())
			
			result = new_rig_object
		else:
			result['result'] = 'Usage: "getdata" to get data. '

		self.wfile.write(json.dumps(result, indent=2))

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

		self._t = threading.Thread(target=_populateQueue, args=(self._s, self._q))
		self._t.daemon = True
		self._t.start()

	def readline(self, timeout=None):
		try:
			line = self._q.get(block=timeout is not None, timeout=timeout)
			print line
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
	def setCoreClock(self, adapterIndex, newPercent):
		raise Exception("not implemented")
	def setCoreClocks(self, newClock):
		raise Exception("not implemented")
	def updateCardData(self):
		raise Exception("not implemented")

''' Requires adl3 in /opt/scripts/adl3/ '''
class AmdApi(GpuApi):
	def __init__(self):
		super(AmdApi, self).__init__()
		os.system('amdconfig --od-enable')
	def getDefaultClocks(self):
		isSaved = os.path.isfile('/run/ethmon/clocks.json')
		if not isSaved:
			cardData = self.getAmdconfigData()
			newDefaultCoreClocks = {i: card['peak_core_clock'] for i,card in cardData.iteritems()}
			if not os.path.exists('/run/ethmon'):
				os.makedirs('/run/ethmon')
			with open('/run/ethmon/clocks.json', 'wb') as clocksFile:
				clocksFile.write(json.dumps(newDefaultCoreClocks, indent=4))
		with open('/run/ethmon/clocks.json', 'rb') as clocksFile:
			return json.loads(clocksFile.read())
	def setFanSpeed(self, adapterIndex, newPercent):
		os.system('/opt/scripts/adl3/atitweak -A {adapterIndex} -f {percent} >/dev/null'.format(
			percent=int(newPercent), 
			adapterIndex=int(adapterIndex)
		))
	def setFanSpeeds(self, newPercent):
		os.system('/opt/scripts/adl3/atitweak -f {percent} >/dev/null'.format(percent=int(newPercent)))
	def setCoreClock(self, adapterIndex, newClock):
		os.system('amdconfig --adapter={adapterIndex} --od-setclocks={clock},0 >/dev/null'.format(
			clock=int(newClock), 
			adapterIndex=int(adapterIndex)
		))
	def setCoreClocks(self, newClock):
		os.system('amdconfig --adapter=all --od-setclocks={clock},0 >/dev/null'.format(clock=int(newClock)))
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
				#print json.dumps(newCardData, indent=4)
				self.cardData = newCardData
			except Exception as e:
				print "Error while updating card data: " + str(e)

			sleepTime = 10 - min(10, time.time()-startTime)
			time.sleep(sleepTime)
	def getAtitweakData(self):
		o,e = subprocess.Popen('/opt/scripts/adl3/atitweak -s', stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True).communicate()
		if "Segmentation fault" in e:
			print "adl crash detected, rebooting..."
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
		#print json.dumps(newCardData)
		return newCardData

#dummy for now. allows to monitor hashrates only
class NvidiaApi(GpuApi):
	def getDefaultClocks(self):
		return {}
	def setFanSpeed(self, adapterIndex, newPercent):
		return
	def setFanSpeeds(self, newPercent):
		return
	def setCoreClock(self, adapterIndex, newClock):
		return
	def setCoreClocks(self, newClock):
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
