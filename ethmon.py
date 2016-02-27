#!/usr/bin/env python
# -*- coding: utf-8 -*-

#author Philon
ETHMON_VERSION=0.51

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

rig_object = dict()
should_do_restart = False

def getConfig():
	if len(sys.argv) == 1:
		print 'must specify a config as parameter, aborting...'
		exit()

	configUrl = sys.argv[1]
	try:
		with open(configUrl) as confFile:
			try:
				return json.loads(confFile.read())
			except ValueError:
				print 'config is invalid json, aborting...'
				exit()
	except IOError:
		print 'ethminer.conf not found, aborting...'
		exit()

def getSystemUptime():
	with open('/proc/uptime', 'r') as f:
		return int(float(f.readline().split()[0]))

def isProgramRunning(name):
	out = subprocess.Popen('ps aux | grep {name}'.format(name=name), stdout=subprocess.PIPE, shell=True).communicate()[0]
	for t in out.split('\n'):
		if len(t)>0 and not 'grep' in t:
			return True
	return False

def calcFanSpeedFromTemp(temp):
	#min 50% @ 50°, max 100% @ 100°
	return min(100, max(50, temp))

''' Starts/Stops the mining process and other services '''
class Ethmon(object):
	def __init__(self):
		self.minerProcess = None
		self.server = EthmonServer()
		self.outputReader = EthminerOutputReader()
		self.server.start(8042)
		self.gpuApi = AtiApi()

		self.poolUrl = ''
		self.startTime = time.time()

	def start(self):
		config = getConfig()
		self.poolUrl = config['poolUrl']
		os.system('killall ethminer')
		self.minerProcess = subprocess.Popen(
			'ethminer -G -F ' + self.poolUrl,
			stderr=subprocess.PIPE,
			shell=True
		)
		self.outputReader.start(self.minerProcess.stderr)

	def stop(self):
		self.outputReader.stop()
		self.minerProcess.kill()
		os.system('killall ethminer')

	def restart(self):
		self.stop()
		self.start()

	def mainLoop(self):
		crashTestTimer = 0
		updateTimer = 0
		fanTimer = 0
		while True:
			#check for ethminer crash
			crashTestTimer += 1
			if crashTestTimer >= 10:
				crashTestTimer = 0
				if not isProgramRunning('ethminer'):
					self.restart()

			#update rig_object
			updateTimer += 1
			if updateTimer >= 5:
				self.updateRigObject()
				updateTimer = 0

			#adjust fan speeds
			fanTimer += 1
			if fanTimer >= 30:
				fanTimer = 0
				avgTemp = 0
				numCards = 0
				for card in rig_object['miners']:
					if card['temperature'] == -1.0: continue
					avgTemp += card['temperature']
					numCards += 1
				avgTemp /= numCards if numCards>0 else 1
				newSpeed = calcFanSpeedFromTemp(avgTemp)
				self.gpuApi.setFanSpeeds(newSpeed)
				print "Avg Temp is ", avgTemp, ", setting fans to ", newSpeed, "%"

			#check for requested restart
			global should_do_restart
			if should_do_restart:
				should_do_restart = False
				self.restart()

			time.sleep(1)

	def updateRigObject(self):
		new_rig_object = dict()
		new_rig_object['poolUrl'] = self.poolUrl
		new_rig_object['firmwareVersion'] = ETHMON_VERSION

		totalMhs = self.outputReader.getMhs()
		cardData = self.gpuApi.getCardData()
		mhsPerCard = totalMhs / (len(cardData) if len(cardData)>0 else 1)
		new_rig_object['miners'] = []
		for card in cardData:
			miner = card.copy()
			miner['name'] = str(card['adapter_nr']) + ' - ' + card['name']
			miner['mhs'] = mhsPerCard
			miner['elapsedSecs'] = time.time() - self.startTime
			new_rig_object['miners'].append(miner)

		global rig_object
		rig_object = new_rig_object

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
		print time.asctime(), 'Stopped server'

	def server_thread(self):
		print time.asctime(), 'Started Server'
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
			result = rig_object
		elif cmd == 'restart':
			global should_do_restart
			should_do_restart = True
			result['result'] = 'Restarting ethminer now.'
		else:
			result['result'] = 'Usage: "getdata" for data, "restart" to restart ethminer. '

		self.wfile.write(json.dumps(result, indent=2))

'''Starts ethminer process and continuously reads data from its stdout'''
class EthminerOutputReader(object):
	def __init__(self):
		self.should_stop = False
		self.sr = None
		self.t = None

		self.mhs = 0
		self.lastMhsTime = 0

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
			return 0
		return self.mhs

	def stop(self):
		self.should_stop = True
		self.t.join()

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
class GpuApi:
	def getCardData(self):
		raise Exception("not implemented")
	def setFanSpeeds(self, newPercent):
		raise Exception("not implemented")

''' Requires adl3 in /opt/scripts/adl3/ '''
class AtiApi(GpuApi):
	def __init__(self):
		self.cardData = []
		self.cardDataThread = threading.Thread(target=self.updateCardData)
		self.cardDataThread.daemon = True
		self.cardDataThread.start()

	def getCardData(self):
		return self.cardData

	def setFanSpeeds(self, newPercent):
		workThread = threading.Thread(target=self._setFanSpeedsImpl, args=(newPercent,))
		workThread.daemon = True
		workThread.start()

	def _setFanSpeedsImpl(self, newPercent):
		os.system('/opt/scripts/adl3/atitweak -f {percent}'.format(percent=int(newPercent)))

	def updateCardData(self):
		while True:
			startTime = time.time()
			t = subprocess.Popen('/opt/scripts/adl3/atitweak -s', stdout=subprocess.PIPE, shell=True).communicate()[0]
			adapters = zip(*re.findall(r'(\d)\. (.*?)\W*\(:', t))
			if len(adapters)==0: return []
			adapterNrs = list(adapters[0])
			names = list(adapters[1])
			temps = re.findall(r'temperature ([0-9\.]*) C', t)
			fans = zip(*re.findall(r'(fan speed ([0-9\.]*)%)|(unable to get fan speed)', t))
			if fans==[]: return []
			fans = list(fans[1])
			fans = [-1.0 if fan=='' else float(fan) for fan in fans]
			clocks = re.findall(r'engine clock ([0-9\.]*)MHz', t)
			voltages = re.findall(r'core voltage ([0-9\.]*)VDC', t)

			newCardData = list()
			for i in range(len(adapterNrs)):
				card = dict()
				card['adapter_nr'] = int(adapterNrs[i])
				card['name'] = names[i]
				card['temperature'] = float(temps[i])
				card['fan_percent'] = float(fans[i])
				card['clock'] = float(clocks[i])
				card['voltage'] = float(voltages[i])
				newCardData.append(card)
			self.cardData = newCardData

			sleepTime = 30 - min(30, time.time()-startTime)
			time.sleep(sleepTime)

if __name__ == '__main__':
	ethmon = Ethmon()
	ethmon.start()
	try:
		ethmon.mainLoop()
	finally:
		ethmon.stop()
