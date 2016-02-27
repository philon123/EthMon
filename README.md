Ethmon is a monitoring tool for Ethminer. It reads Ethminers output and provides useful data over a web api.
Note that newer version of Ethminer don't give any commandline output. This tool relies on this output though, so you need the older version. 

- Provides overall status data and for each card separately. Json format. Call http://IP_OF_MINER:8042?cmd=getdata
- Remote restarting of miner with http://IP_OF_MINER:8042?cmd=restart
- Automatically restarts Ethminer if it crashes.
- Automatic fanspeed tuning to protect your cards.

Only ATI cards are supported at the moment.

Requires adl3 in /opt/scripts/adl3. No need to install it, just download from github and unpack.
https://github.com/mjmvisser/adl3/