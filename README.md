#What is this?

Ethmon is a monitoring tool for Ethminer. Unfortunately, Ethminer does not offer any kind of api. Also, some AMD cards will not automatically adjust fanspeeds! This project aims to fill these gaps and to allow you to lean back and watch the ether roll in :)


#Installation

* Clone this repo into /opt/ethmon
* Requires adl3 in /opt/scripts/adl3. No need to install it, just download from github and unpack: https://github.com/mjmvisser/adl3/
* Run with 'python /opt/ethmon/ethmon.py /opt/ethmon/ethmon.conf'


#Changelog / feature list

v0.7.7
* Allow setting of custom ethminer parameters in config
* Better error recovery

v0.7.6
* Removed redundant output
* Fixed order of cards in getdata output
* Smoother hashrate reporting

v0.7.5
* Added api command 'getconfig' that returns the currently used config including default values.
* Throw error when no cards are detected at launch
* Throw error when detecting that not all gpus have the same default core clock
* Option to set --farm-recheck. If unspecified or 0, will not pass this option

v0.7.4
* Option to undervolt cards. This will only work when using an unlocked BIOS. WARNING: You can burn your cards by setting a voltage larger than the default!
* Option to change the default mem clock. This value will be set once and stay static.
* Nicer output in terminal and other various fixes

v0.7.1
* Option to set max/min core clock. Use this to limit power consumption of your cards.

v0.7.0
* Automatic down clocking of cards that are running too hot. Will up clock if temps allow.

v0.6.2
* Automatic restart on ethminer hang (no output for 20min)
* Automatic restart on graphics driver crash
* Bugfix: Better parsing of driver output
* If no cards detected, report hashrate on a "DUMMY CARD".

v0.6.0
* Semantic versioning
* Experimental support for nvidia cards (only reports hashrate). Change the config parameter "gpuApi" to "nvidia" to try this

v0.55
* Automatic and configurable fan speed adjustment for each card to protect your cards
* Automatically restarts Ethminer on crashes.

v0.51
* JSON api for remote monitoring
* Provides data for the rig and each card separately. Json format. Call http://IP_OF_MINER:8042?cmd=getdata
* Remote restarting of miner with http://IP_OF_MINER:8042?cmd=restart
