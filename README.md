## WojakTip - WojakCoin ($WJK) tipbot for Telegram
 
### Dependencies 

* `apt-get install python-dev`
* `apt-get install python-pip`
* `pip install python-telegram-bot --upgrade`
* `pip install requests`
* `pip install emoji`


In order to run the tip-bot a WojakCoin daemon is needed (wojakcoind). Use your **existing** `wojakcoin.conf` (e.g. in `~/.wojakcoin/wojakcoin.conf` or `/root/.wojakcoin/wojakcoin.conf`). No extra .conf or .service files are required.

### Configuration file

Create a `config.json` **JSON** file and set:

* `rpc-uri`: `http://127.0.0.1:20760` (WojakCoin default RPC port)
* `rpc-user`, `rpc-psw`: Same as in your existing **wojakcoin.conf** (`rpcuser` / `rpcpassword`)

(sample)
 
    {
    	"telegram-token": "YOUR_BOT_TOKEN",
    	"telegram-botname": "WojakTip",
    	"rpc-uri": "http://127.0.0.1:20760",
    	"rpc-user": "wojakcoinrpc",
    	"rpc-psw": "YOUR_RPC_PASSWORD_FROM_wojakcoin.conf",
    	"admins": [-0, 0],
    	"spam_filter": [5, 60]
    }

* `telegram-token`: Your bot's token from [@BotFather](https://t.me/BotFather).
* `rpc-uri`: WojakCoin daemon RPC (default port **20760**). Keep it local (127.0.0.1).
* `rpc-user`, `rpc-psw`: From your existing **wojakcoin.conf**.
* `admins`: Telegram UserIDs of bot admins.
* `spam_filter`: `[5, 60]` = max 5 actions per 60 seconds.

### Daemon config

Use your **existing** wojakcoin.conf. Ensure it has at least:

* `server=1`
* `rpcuser=...` and `rpcpassword=...` (use those values in config.json)
* `rpcport=20760` (or match the port in config.json `rpc-uri`)
* `rpcallowip=127.0.0.1` (or as needed)

No .service or extra config files are used; run wojakcoind yourself, then start the bot.

---

### ToDo

- [x] Add service commands like `/pause`
- [x] Populate `strings.json`
- [x] Add spam protection
- [ ] Per-user language
