#What is this?

Ethmon is a monitoring tool for Ethminer. Unfortunately, Ethminer does not offer any kind of api. Also, current AMD drivers do not automatically set fanspeeds! This project aims to fill these gaps and to allow you to lean back and watch the ether roll in :)

*Automatic and configurable fanspeed adjustment for each card to protect your cards
*Automatically restarts Ethminer if it crashes.
*JSON api for remote monitoring
*Provides data for the rig and each card separately. Json format. Call http://IP_OF_MINER:8042?cmd=getdata
*Remote restarting of miner with http://IP_OF_MINER:8042?cmd=restart

Only AMD cards are supported at the moment.

#Installation

* Requires adl3 in /opt/scripts/adl3. No need to install it, just download from github and unpack: https://github.com/mjmvisser/adl3/
* Clone this repo into /opt/ethmon
* Run with python /opt/ethmon/ethmon.py /opt/ethmon/ethmon.conf
