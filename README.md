#What is this?

Ethmon is a monitoring tool for Ethminer. Unfortunately, Ethminer does not offer any kind of api. Also, some AMD cards will not automatically adjust fanspeeds! This project aims to fill these gaps and to allow you to lean back and watch the ether roll in :)


#Installation

* Clone this repo into /opt/ethmon
* Requires adl3 in /opt/scripts/adl3. No need to install it, just download from github and unpack: https://github.com/mjmvisser/adl3/
* Run with python /opt/ethmon/ethmon.py /opt/ethmon/ethmon.conf


#Changelog / feature list

v0.6.2
* Automatic restart on ethminer hang (no output for 20min)
* Automatic restart on detected adl crash
* Bugfix: Better parsing of driver output
* If no cards detected, report hashrarte on a "DUMMY CARD". 

v0.6.0
* Semantic versioning
* Experimental support for nvidia cards (only reports hashrate, does not use adl). Change the config parameter "gpuApi" to "nvidia" to try this

v0.55
* Automatic and configurable fanspeed adjustment for each card to protect your cards
* Automatically restarts Ethminer on crashes.

v0.51
* JSON api for remote monitoring
* Provides data for the rig and each card separately. Json format. Call http://IP_OF_MINER:8042?cmd=getdata
* Remote restarting of miner with http://IP_OF_MINER:8042?cmd=restart