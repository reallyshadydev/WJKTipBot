#!/usr/bin/python
#coding=utf-8

import warnings
warnings.filterwarnings("ignore", message=".*deprecated.*")

import emoji
from telegram.ext import Updater
from telegram.ext import CommandHandler, CallbackQueryHandler, MessageHandler
from telegram.ext import filters
from telegram import ParseMode, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, TimedOut, RetryAfter
from PandaRPC import PandaRPC, Wrapper as RPCWrapper
from HelperFunctions import *
import logging
logging.basicConfig(
	format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
	level=logging.INFO
)
import time
import threading
import requests
from datetime import datetime


config = load_file_json("config.json")
if config.get("debug"):
	logging.getLogger().setLevel(logging.DEBUG)
	for _h in logging.getLogger().handlers:
		_h.setLevel(logging.DEBUG)
_debug_log = logging.getLogger(__name__)
_lang = "en"  # ToDo: Per-user language
strings = Strings("strings.json")
_paused = False
_spam_filter = AntiSpamFilter(config["spam_filter"][0], config["spam_filter"][1])
_rain_queues = {
	"-1": [("0", "@username", "Name")]
}

# Constants
__wallet_rpc = RPCWrapper(PandaRPC(config["rpc-uri"], (config["rpc-user"], config["rpc-psw"])))
__rain_queue_filter = filters.Filters.group & (
		filters.Filters.text | filters.Filters.photo | filters.Filters.video | filters.Filters.reply | filters.Filters.forwarded
	)
__rain_queue_min_text_length = 1   # minimum characters in a message to count as active
__rain_queue_min_words = 1         # minimum words in a message to count as active
__rain_queue_max_members = 30  # Max members in a queue, 30
__rain_min_members = 1  # minimum active members in queue for /rain
__rain_min_amount = 1  # minimum WJK per member for /rain

# Block announce (new block -> message to group)
_last_block_count = -1
_block_announce_stop = False
BLOCK_EXPLORER_URL = "https://explorer.wojakcoin2017.xyz"
EXPLORER_API_BASE = "https://explorer.wojakcoin2017.xyz/api"


def _fetch_block_from_explorer_api(block_hash):
	"""Fetch block info from Explorer REST API (Esplora-compatible). Returns dict with 'time' (Unix) and 'tx_count' or None."""
	_debug_log.debug("_fetch_block_from_explorer_api hash=%s", block_hash[:16] + "..." if len(block_hash) > 16 else block_hash)
	try:
		r = requests.get("%s/block/%s" % (EXPLORER_API_BASE, block_hash), timeout=10)
		if r.status_code != 200:
			return None
		data = r.json()
		if not isinstance(data, dict):
			return None
		out = {}
		# Esplora/electrs: timestamp (Unix), tx_count or similar
		ts = data.get("timestamp") or data.get("time")
		if ts is not None:
			out["time"] = int(ts)
		tx_count = data.get("tx_count") or data.get("n_tx")
		if tx_count is not None:
			out["tx_count"] = int(tx_count)
		elif isinstance(data.get("tx"), list):
			out["tx_count"] = len(data["tx"])
		return out if out else None
	except Exception:
		return None


def _check_new_blocks(bot, chat_id):
	"""Poll RPC for new blocks and send one message per new block to chat_id."""
	global _last_block_count
	_debug_log.debug("_check_new_blocks chat_id=%s last_block_count=%s", chat_id, _last_block_count)
	r = __wallet_rpc.getblockcount()
	if not r.get("success") or r.get("result", {}).get("error") is not None:
		_debug_log.debug("_check_new_blocks getblockcount failed or error: %s", r)
		return
	try:
		count = int(r["result"]["result"])
	except (TypeError, KeyError):
		_debug_log.debug("_check_new_blocks getblockcount result parse failed: %s", r)
		return
	if _last_block_count < 0:
		_last_block_count = count
		_debug_log.debug("_check_new_blocks init last_block_count=%s", count)
		return
	if count <= _last_block_count:
		return
	for height in range(_last_block_count + 1, count + 1):
		rh = __wallet_rpc.getblockhash(height)
		if not rh.get("success") or rh.get("result", {}).get("error") is not None:
			continue
		block_hash = rh["result"]["result"]
		time_str = "—"
		n_tx = "—"
		for verbosity in (2, 1):
			rb = __wallet_rpc.getblock(block_hash, verbosity)
			if not rb.get("success") or rb.get("result", {}).get("error") is not None:
				continue
			res = rb["result"]
			block = res.get("result") if isinstance(res.get("result"), dict) else res
			if isinstance(block, dict):
				tx_list = block.get("tx", [])
				n_tx = len(tx_list)
				bt = block.get("time")
				if bt is not None:
					time_str = datetime.utcfromtimestamp(bt).strftime("%Y-%m-%d %H:%M:%S UTC")
			break
		# If RPC didn't give time or tx count, try Explorer API (https://explorer.wojakcoin2017.xyz/api-docs)
		if (time_str == "—" or n_tx == "—") and block_hash:
			api_block = _fetch_block_from_explorer_api(block_hash)
			if api_block:
				if time_str == "—" and api_block.get("time") is not None:
					time_str = datetime.utcfromtimestamp(api_block["time"]).strftime("%Y-%m-%d %H:%M:%S UTC")
				if n_tx == "—" and api_block.get("tx_count") is not None:
					n_tx = api_block["tx_count"]
		msg = (
			"New block #%s\n"
			"Hash: `%s`\n"
			"Time: %s\n"
			"Transactions: %s\n"
			"[View on explorer](%s/block/%s)"
		) % (height, block_hash, time_str, n_tx, BLOCK_EXPLORER_URL, block_hash)
		_debug_log.debug("_check_new_blocks sending block #%s hash=%s time=%s n_tx=%s", height, block_hash[:12], time_str, n_tx)
		try:
			bot.send_message(
				chat_id=chat_id,
				text=msg,
				parse_mode=ParseMode.MARKDOWN,
				disable_web_page_preview=True
			)
		except Exception as e:
			logging.warning("Block announce send failed: %s", e)
	_last_block_count = count


def _block_announce_loop(updater, chat_id):
	"""Background loop: every 30s check for new blocks and announce to chat_id."""
	global _block_announce_stop
	_debug_log.debug("_block_announce_loop started chat_id=%s", chat_id)
	while not _block_announce_stop:
		try:
			_check_new_blocks(updater.bot, chat_id)
		except Exception as e:
			logging.warning("Block check failed: %s", e)
		for _ in range(30):
			if _block_announce_stop:
				break
			time.sleep(1)


# ToDo: Add service commands to check the health of the daemon / wallet.


def cmd_start(update, context):
	_debug_log.debug("cmd_start user=%s chat=%s args=%s", update.effective_user.id, getattr(update.effective_chat, "id", None), getattr(context, "args", None))
	"""Reacts when /start is sent to the bot."""
	args = context.args or []
	if update.effective_chat.type == "private":
		if not _spam_filter.verify(str(update.effective_user.id)):
			return
		# Check for deep link
		if len(args) > 0:
			if args[0].lower() == "about":
				cmd_about(update, context)
			elif args[0].lower() == "help":
				cmd_help(update, context)
			elif args[0].lower() == "address":
				deposit(update, context)
			else:
				update.message.reply_text(
					strings.get("error_bad_deep_link", _lang),
					quote=True,
					parse_mode=ParseMode.MARKDOWN,
					disable_web_page_preview=True
				)
		else:
			_button_help = InlineKeyboardButton(
				text=emoji.emojize(strings.get("button_help", _lang), language='alias'),
				callback_data="help"
			)
			_button_about = InlineKeyboardButton(
				text=emoji.emojize(strings.get("button_about", _lang), language='alias'),
				callback_data="about"
			)
			_markup = InlineKeyboardMarkup(
				[
					[_button_help, _button_about]
				]
			)
			update.message.reply_text(
				emoji.emojize(strings.get("welcome", _lang), language='alias'),
				quote=True,
				parse_mode=ParseMode.MARKDOWN,
				disable_web_page_preview=True,
				reply_markup=_markup
			)


def cmd_about(update, context):
	_debug_log.debug("cmd_about user=%s", update.effective_user.id)
	bot = context.bot
	if not _spam_filter.verify(str(update.effective_user.id)):
		return
	if update.effective_chat is None:
		_chat_type = "private"
	elif update.effective_chat.type == "private":
		_chat_type = "private"
	else:
		_chat_type = "group"
	#
	if _chat_type == "private":
		# Check if callback
		try:
			if update.callback_query.data is not None:
				update.callback_query.answer(strings.get("callback_simple", _lang))
		except:
			pass
		# Answer
		_button = InlineKeyboardButton(
			text=emoji.emojize(strings.get("button_help", _lang), language='alias'),
			callback_data="help"
		)
		_markup = InlineKeyboardMarkup(
			[[_button]]
		)
		bot.send_message(
			chat_id=update.effective_chat.id,
			text=strings.get("about", _lang),
			parse_mode=ParseMode.MARKDOWN,
			disable_web_page_preview=True,
			reply_markup=_markup
		)
	else:
		# Done: Button (2018-07-18)
		_button = InlineKeyboardButton(
			text=emoji.emojize(strings.get("button_about", _lang), language='alias'),
			url="https://telegram.me/%s?start=about" % bot.username
		)
		_markup = InlineKeyboardMarkup(
			[[_button]]
		)
		update.message.reply_text(
			"%s" % strings.get("about_public", _lang),
			parse_mode=ParseMode.MARKDOWN,
			disable_web_page_preview=True,
			reply_markup=_markup
		)
	return True


def cmd_help(update, context):
	_debug_log.debug("cmd_help user=%s", update.effective_user.id)
	bot = context.bot
	if not _spam_filter.verify(str(update.effective_user.id)):
		return
	# Check if callback (e.g. from inline button)
	try:
		if update.callback_query is not None and getattr(update.callback_query, "data", None) is not None:
			update.callback_query.answer(strings.get("callback_simple", _lang))
	except Exception:
		pass
	# Send full help in both private and group/supergroup
	_button = InlineKeyboardButton(
		text=emoji.emojize(strings.get("button_help_advanced_caption", _lang), language='alias'),
		url=strings.get("button_help_advanced_url", _lang)
	)
	_markup = InlineKeyboardMarkup(
		[[_button]]
	)
	help_text = emoji.emojize(strings.get("help", _lang), language='alias')
	chat_id = update.effective_chat.id if update.effective_chat else update.effective_user.id
	bot.send_message(
		chat_id=chat_id,
		text=help_text,
		parse_mode=ParseMode.MARKDOWN,
		reply_markup=_markup,
		disable_web_page_preview=True
	)
	return True


def cmd_links(update, context):
	_debug_log.debug("cmd_links user=%s", update.effective_user.id)
	"""Send official WojakCoin links."""
	text = """*Official WojakCoin Links*
•  Website: https://wojakcoin.cash/
•  X (Twitter): https://x.com/WojakCoin2017
•  Telegram: https://t.me/wojakcoin2017
•  Discord: https://discord.gg/QyPKPJhgcR
•  GitHub: https://github.com/WojakCoinProj/wojakcore
•  Web Wallet: https://wojakcoin.cash/wallet
•  Block Explorer #1: https://wojak-explorer.dedoo.xyz/
•  Block Explorer #2: https://explorer.wojakcoin2017.xyz/

*Mining Pools*
•  rt-pool: https://rt-pool.cc/
•  zpool: https://zpool.ca/
•  wjksolo (shared): https://kriptokyng.com/pools/wjk
•  wjksolo Solo: https://kriptokyng.com/pools/wjksolo
•  AU Merged Mining (Cminors): https://au-merged-mine.cminors-pool.com/site/mining

*Price Aggregator*
•  LiveCoinWatch: https://www.livecoinwatch.com/price/WojakCoin-WJK"""
	update.message.reply_text(
		text,
		parse_mode=ParseMode.MARKDOWN,
		disable_web_page_preview=True
	)


def cmd_buy(update, context):
	_debug_log.debug("cmd_buy user=%s", update.effective_user.id)
	"""Send exchange / buy links from wojakcoin.cash/exchanges."""
	text = """*Buy WojakCoin (WJK)*

*Official*
•  Website (Buy Now): https://wojakcoin.cash/
•  Exchanges page: https://wojakcoin.cash/exchanges
•  Wallets: https://wojakcoin.cash/wallets

*Exchanges*
•  NestEx (WJK/USDT): https://trade.nestex.one/spot/WJK
•  KlingEx (WJK-USDT): https://klingex.io/trade/WJK-USDT
•  Rabid Rabbit Exchange (WJK-USDT): https://rabid-rabbit.org/account/trade/WJK-USDT
•  GateVia (WJK/LTC, WJK/DOGE): https://gatevia.io/
•  Komodo (WJK wallet): https://app.komodoplatform.com/wallet/wjk
•  GLEEC (WJK wallet/DEX): https://dex.gleec.com/wallet/wjk"""
	update.message.reply_text(
		text,
		parse_mode=ParseMode.MARKDOWN,
		disable_web_page_preview=True
	)


def cmd_wallets(update, context):
	_debug_log.debug("cmd_wallets user=%s", update.effective_user.id)
	"""Send wallet links from wojakcoin.cash/wallets."""
	text = """*WojakCoin (WJK) Wallets*

*Official*
•  WojakCore (full node, desktop): https://github.com/wojakcoinproj/wojakcore
•  Web Wallet: https://wojakcoin.cash/wallet
•  Android Wallet (APK): https://wojakcoin.cash/wojakwallet.apk

*Third-party*
•  Komodo: https://app.komodoplatform.com/wallet/wjk
•  GLEEC: https://dex.gleec.com/wallet/wjk

Full list: https://wojakcoin.cash/wallets"""
	update.message.reply_text(
		text,
		parse_mode=ParseMode.MARKDOWN,
		disable_web_page_preview=True
	)


def _nestex_last_wjk_price():
	"""Fetch WJK last price from NestEx public ticker API (no auth). Returns float or None."""
	url = "https://trade.nestex.one/api/cg/tickers/WJK_USDT"
	try:
		r = requests.get(url, timeout=15)
		r.raise_for_status()
		data = r.json()
	except (requests.RequestException, ValueError):
		return None
	price = data.get("last_price")
	if price is None:
		return None
	try:
		return float(price)
	except (TypeError, ValueError):
		return None


def cmd_price(update, context):
	_debug_log.debug("cmd_price user=%s", update.effective_user.id)
	"""Fetch WJK price from LiveCoinWatch and NestEx (public ticker), send to chat."""
	api_key = config.get("livecoinwatch-api-key")
	if not api_key:
		update.message.reply_text("Price feed not configured (missing API key).", quote=True)
		return
	url = "https://api.livecoinwatch.com/coins/single"
	headers = {
		"content-type": "application/json",
		"x-api-key": api_key,
	}
	payload = {"currency": "USD", "code": "WJK", "meta": True}
	try:
		r = requests.post(url, headers=headers, json=payload, timeout=15)
		r.raise_for_status()
		data = r.json()
	except requests.RequestException as e:
		err_msg = str(e)
		if "timed out" in err_msg.lower() or "timeout" in err_msg.lower():
			update.message.reply_text("Price API timed out. Try again in a moment.", quote=True)
		else:
			update.message.reply_text("Could not fetch price: %s" % err_msg, quote=True)
		return
	except ValueError:
		update.message.reply_text("Invalid response from price API.", quote=True)
		return
	rate = data.get("rate")
	name = data.get("name", "WJK")
	nestex_price = _nestex_last_wjk_price()
	if rate is None:
		# Use NestEx as live rate when we have it (clean "Price: $X.XX" message)
		if nestex_price is not None:
			price_usd = nestex_price
			if price_usd < 0.0001:
				price_str = "%.8f" % price_usd
			elif price_usd < 1:
				price_str = "%.6f" % price_usd
			else:
				price_str = "%.2f" % price_usd
			supply = data.get("totalSupply") or data.get("circulatingSupply")
			cap_str = ""
			if supply and float(supply) > 0:
				cap_str = "\nMarket cap: $%s" % ("{:,.0f}".format(price_usd * float(supply)))
			update.message.reply_text(
				"*%s* (WJK)\nPrice: $%s USD%s" % (name, price_str, cap_str),
				quote=True,
				parse_mode=ParseMode.MARKDOWN
			)
			return
		# No live rate from LCW or NestEx; show ATH as fallback if available
		ath = data.get("allTimeHighUSD")
		if ath is not None and float(ath) > 0:
			price_usd = float(ath)
			if price_usd < 0.0001:
				price_str = "%.8f" % price_usd
			elif price_usd < 1:
				price_str = "%.6f" % price_usd
			else:
				price_str = "%.2f" % price_usd
			supply = data.get("totalSupply") or data.get("circulatingSupply")
			cap_str = ""
			if supply and float(supply) > 0:
				cap_str = "\nMarket cap: $%s" % ("{:,.0f}".format(price_usd * float(supply)))
			update.message.reply_text(
				"*%s* (WJK)\nPrice: $%s USD%s" % (name, price_str, cap_str),
				quote=True,
				parse_mode=ParseMode.MARKDOWN
			)
			return
		fallback = []
		if ath is not None:
			fallback.append("ATH: $%.8f" % float(ath))
		if data.get("totalSupply"):
			fallback.append("Supply: %s" % "{:,.0f}".format(float(data["totalSupply"])))
		fallback.append("(no markets on LiveCoinWatch)")
		update.message.reply_text("Price not available for %s.\n%s" % (name, " | ".join(fallback)), quote=True)
		return
	price_usd = float(rate)
	# Format: 8 decimals if small, else 2
	if price_usd < 0.0001:
		price_str = "%.8f" % price_usd
	elif price_usd < 1:
		price_str = "%.6f" % price_usd
	else:
		price_str = "%.2f" % price_usd
	delta = data.get("delta") or {}
	hour_pct = delta.get("hour")
	if hour_pct is not None:
		hour_pct = (float(hour_pct) - 1) * 100
	cap = data.get("cap")
	# When API returns cap as null, estimate from rate * supply (e.g. WJK)
	if cap is None or cap <= 0:
		supply = data.get("circulatingSupply") or data.get("totalSupply")
		if supply is not None and float(supply) > 0:
			cap = price_usd * float(supply)
		else:
			cap = None
	cap_str = ""
	if cap is not None and cap > 0:
		cap_str = "\nMarket cap: $%s" % ("{:,.0f}".format(cap))
	change_line = "\n1h change: %+.2f%%" % hour_pct if hour_pct is not None else ""
	text = "*%s* (WJK)\nPrice: $%s USD%s%s" % (name, price_str, cap_str, change_line)
	update.message.reply_text(
		text,
		parse_mode=ParseMode.MARKDOWN,
		disable_web_page_preview=True,
		quote=True
	)


def _format_hashrate(hps):
	"""Format hashes per second as H/s, KH/s, MH/s, GH/s, or TH/s."""
	if hps is None or hps <= 0:
		return None
	if hps >= 1e12:
		return "%.2f TH/s" % (hps / 1e12)
	if hps >= 1e9:
		return "%.2f GH/s" % (hps / 1e9)
	if hps >= 1e6:
		return "%.2f MH/s" % (hps / 1e6)
	if hps >= 1e3:
		return "%.2f KH/s" % (hps / 1e3)
	return "%.0f H/s" % hps


def cmd_mining(update, context):
	_debug_log.debug("cmd_mining user=%s", update.effective_user.id)
	"""Show WojakCoin network mining stats from RPC."""
	height = None
	r = __wallet_rpc.getblockcount()
	if r.get("success") and r.get("result", {}).get("error") is None:
		try:
			height = int(r["result"]["result"])
		except (TypeError, KeyError):
			pass
	diff = None
	r = __wallet_rpc.getblockchaininfo()
	if r.get("success") and r.get("result", {}).get("error") is None:
		try:
			res = r["result"].get("result")
			if isinstance(res, dict):
				diff = res.get("difficulty")
			if diff is not None:
				diff = float(diff)
		except (TypeError, KeyError, ValueError):
			pass
	hashrate = None
	r = __wallet_rpc.getnetworkhashps(120)
	if r.get("success") and r.get("result", {}).get("error") is None:
		try:
			hashrate = float(r["result"]["result"])
		except (TypeError, KeyError, ValueError):
			pass
	lines = ["*WojakCoin (WJK) – Mining*"]
	if height is not None:
		lines.append("Block height: %s" % "{:,}".format(height))
	if diff is not None:
		if diff >= 1e6:
			lines.append("Difficulty: %s" % ("%.2fM" % (diff / 1e6)))
		elif diff >= 1e3:
			lines.append("Difficulty: %s" % ("%.2fK" % (diff / 1e3)))
		else:
			lines.append("Difficulty: %s" % ("%.4f" % diff))
	if hashrate is not None:
		hr_str = _format_hashrate(hashrate)
		if hr_str:
			lines.append("Network hashrate: %s" % hr_str)
	lines.append("")
	lines.append("Block time: ~2 min  •  Algo: SHA-256")
	lines.append("*Pools:* rt-pool, zpool, wjksolo, Cminors merged")
	lines.append("/links – pool URLs")
	text = "\n".join(lines)
	update.message.reply_text(
		text,
		parse_mode=ParseMode.MARKDOWN,
		disable_web_page_preview=True,
		quote=True
	)


def msg_no_account(update, context):
	bot = context.bot
	_button = InlineKeyboardButton(
		text=emoji.emojize(strings.get("user_no_address_button", _lang), language='alias'),
		url="https://telegram.me/%s?start=address" % bot.username
	)
	_markup = InlineKeyboardMarkup(
		[[_button]]
	)
	update.message.reply_text(
		"%s" % strings.get("user_no_address", _lang),
		parse_mode=ParseMode.MARKDOWN,
		disable_web_page_preview=True,
		reply_markup=_markup,
	)


def deposit(update, context):
	"""
	This commands works only in private.
	If the user has no address, a new account is created with his Telegram user ID (str)
	"""
	_debug_log.debug("deposit user=%s chat_type=%s", update.effective_user.id, getattr(update.effective_chat, "type", None))
	if update.effective_chat is None:
		_chat_type = "private"
	elif update.effective_chat.type == "private":
		_chat_type = "private"
	else:
		_chat_type = "group"
	# Only show deposit address if it's a private conversation with the bot
	if _chat_type == "private":
		if not _spam_filter.verify(str(update.effective_user.id)):
			return
		if _paused:
			update.message.reply_text(text=emoji.emojize(strings.get("global_paused"), language='alias'), quote=True)
			return
		_username = update.effective_user.username
		if _username is None:
			_user_id = str(update.effective_user.id)
		else:
			_user_id = '@' + _username.lower()
		_address = None
		_rpc_call = __wallet_rpc.getaddressesbyaccount(_user_id)
		if not _rpc_call["success"]:
			print("Error during RPC call.")
			log("deposit", _user_id, "getaddressesbyaccount > Error during RPC call.")
		else:
			if _rpc_call["result"]["error"] is not None:
				print("Error: %s" % _rpc_call["result"]["error"])
				log("deposit", _user_id, "getaddressesbyaccount > Error: %s" % _rpc_call["result"]["error"])
			else:
				# Check if user already has an address. This will prevent creating another address if user has one
				_addresses = _rpc_call["result"]["result"]
				if len(_addresses) == 0:
					# Done: User has no address, request a new one (2018-07-16)
					_rpc_call = __wallet_rpc.getaccountaddress(_user_id)
					if not _rpc_call["success"]:
						print("Error during RPC call.")
						log("deposit", _user_id, "getaccountaddress > Error during RPC call.")
					else:
						if _rpc_call["result"]["error"] is not None:
							print("Error: %s" % _rpc_call["result"]["error"])
							log("deposit", _user_id, "getaccountaddress > Error: %s" % _rpc_call["result"]["error"])
						else:
							_address = _rpc_call["result"]["result"]
				else:
					_address = _addresses[0]
				# ToDo: Can it happen that a user gets more than juan address? Verify.
				if _address is not None:
					update.message.reply_text(
						text="%s `%s`" % (strings.get("user_address", _lang), _address),
						quote=True,
						parse_mode=ParseMode.MARKDOWN,
						disable_web_page_preview=True
					)


# Done: Give balance only if a private chat (2018-07-15)
# Done: Remove WorldCoinIndex (2018-07-15)
def balance(update, context):
	_debug_log.debug("balance user=%s chat_type=%s", update.effective_user.id, getattr(update.effective_chat, "type", None))
	if update.effective_chat is None:
		_chat_type = "private"
	elif update.effective_chat.type == "private":
		_chat_type = "private"
	else:
		_chat_type = "group"
	# Only show balance if it's a private conversation with the bot
	if _chat_type == "private":
		if not _spam_filter.verify(str(update.effective_user.id)):
			return
		if _paused:
			update.message.reply_text(text=emoji.emojize(strings.get("global_paused"), language='alias'), quote=True)
			return
		# See issue #2 (https://github.com/DarthJahus/PandaTip-Telegram/issues/2)
		_username = update.effective_user.username
		if _username is None:
			_user_id = str(update.effective_user.id)
		else:
			_user_id = '@' + _username.lower()
		# get address of user
		_rpc_call = __wallet_rpc.getaddressesbyaccount(_user_id)
		if not _rpc_call["success"]:
			print("Error during RPC call: %s" % _rpc_call["message"])
			log("balance", _user_id, "(1) getaddressesbyaccount > Error during RPC call: %s" % _rpc_call["message"])
		elif _rpc_call["result"]["error"] is not None:
			print("Error: %s" % _rpc_call["result"]["error"])
			log("balance", _user_id, "(1) getaddressesbyaccount > Error: %s" % _rpc_call["result"]["error"])
		else:
			_addresses = _rpc_call["result"]["result"]
			if len(_addresses) == 0:
				# User has no address, ask him to create one
				msg_no_account(update, context)
			else:
				# ToDo: Handle the case when user has many addresses?
				# Maybe if something really weird happens and user ends up having more, we can calculate his balance.
				# This way, when asking for address (/deposit), we can return the first one.
				# Use the account name for balance lookups (matches WojakCoin RPC behaviour)
				_address = _addresses[0]
				_rpc_call = __wallet_rpc.getbalance(_user_id, 0)
				if not _rpc_call["success"]:
					print("Error during RPC call.")
					log("balance", _user_id, "(2) getbalance > Error during RPC call: %s" % _rpc_call["message"])
				elif _rpc_call["result"]["error"] is not None:
					print("Error: %s" % _rpc_call["result"]["error"])
					log("balance", _user_id, "(2) getbalance > Error: %s" % _rpc_call["result"]["error"])
				else:
					_balance = int(_rpc_call["result"]["result"])
					update.message.reply_text(
						text="%s\n`%i WJK`" % (strings.get("user_balance", _lang), _balance),
						parse_mode=ParseMode.MARKDOWN,
						quote=True
					)


def check_active(update, context):
	"""
	Show how many users are currently in the rain queue for this group.
	"""
	_debug_log.debug("check_active user=%s group=%s", update.effective_user.id, getattr(update.effective_chat, "id", None))
	if update.effective_chat is None:
		return
	if update.effective_chat.type not in ["group", "supergroup"]:
		return

	_group_id = str(update.effective_chat.id)

	if _group_id not in _rain_queues or len(_rain_queues[_group_id]) == 0:
		update.message.reply_text(
			text=strings.get("rain_queue_not_enough_members", _lang) % (0, 0, __rain_min_members),
			quote=True,
			parse_mode=ParseMode.MARKDOWN,
			disable_web_page_preview=True
		)
		return

	active_count = len(_rain_queues[_group_id])
	update.message.reply_text(
		text="There are currently *%i* active members in the rain queue (need at least *%i*)." % (active_count, __rain_min_members),
		quote=True,
		parse_mode=ParseMode.MARKDOWN,
		disable_web_page_preview=True
	)


# Done: Rewrite the whole logic; use tags instead of parsing usernames (2018-07-15)
# Done: Allow private tipping if the user can be tagged (@username available) (Nothing to add for it to work.)
def tip(update, context):
	"""
	/tip <user> <amount>
	/tip u1 u2 u3 ... v1 v2 v3 ...
	/tip u1 v1 u2 v2 u3 v3 ...
	"""
	args = context.args or []
	if len(args) < 2:
		return  # To avoid the long annoying error message that's shown when users misuse the command
	if not _spam_filter.verify(str(update.effective_user.id)):
		return
	if _paused:
		update.message.reply_text(text=emoji.emojize(strings.get("global_paused"), language='alias'), quote=True)
		return
	# Get recipients and values
	_message = update.effective_message.text
	_modifier = 0
	_handled = {}
	_recipients = []
	for entity in update.effective_message.entities:
		if entity.type == "text_mention":
			# UserId is unique
			_username = entity.user.name
			if str(entity.user.id) not in _handled:
				_handled[str(entity.user.id)] = (_username, entity.offset, entity.length)
				_recipients.append(str(entity.user.id))
		elif entity.type == "mention":
			# _username starts with @
			# _username is unique
			_username = update.effective_message.text[entity.offset:(entity.offset+entity.length)].lower()
			if _username not in _handled:
				_handled[_username] = (_username, entity.offset, entity.length)
				_recipients.append(_username)
		_part = _message[:entity.offset-_modifier]
		_message = _message[:entity.offset-_modifier] + _message[entity.offset+entity.length-_modifier:]
		_modifier = entity.offset+entity.length-len(_part)
	_debug_log.debug("tip _handled=%s _recipients=%s", _handled, _recipients)
	_amounts = _message.split()
	# check if amounts are all convertible to float
	_amounts_float = []
	try:
		for _amount in _amounts:
			_amounts_float.append(convert_to_int(_amount))
	except:
		_amounts_float = []
	# Make sure number of recipients is the same as number of values
	# old: if len(_amounts_float) != len(_recipients) or len(_amounts_float) == 0 or len(_recipients) == 0:
	# new: ((len(_amounts_float) == len(_recipients)) or (len(_amounts_float) == 1)) and (len(_recipients) > 0),
	# use opposite
	if ((len(_amounts_float) != len(_recipients)) and (len(_amounts_float) != 1)) or (len(_recipients) == 0):
		update.message.reply_text(
			text=strings.get("tip_error_arguments", _lang),
			quote=True,
			parse_mode=ParseMode.MARKDOWN
		)
	else:
		do_tip(update, context, _amounts_float, _recipients, _handled)


def damp_rock(update, context):
	"""
	Manages a queue of active users.
	Activity type is checked before calling this function.
	Message length should be enforced to avoid spam.
	:param bot: Bot
	:param update: Update
	:return: None
	"""
	_debug_log.debug("damp_rock user=%s group=%s text_len=%s", update.effective_user.id, str(update.effective_chat.id) if update.effective_chat else None, len(update.effective_message.text or ""))
	if _paused:
		return
	if update.effective_chat is None:
		return
	elif update.effective_chat.type not in ["group", "supergroup"]:
		return
	#
	_group_id = str(update.effective_chat.id)
	if update.effective_user.is_bot:
		return
	# Slash-commands are not "chat activity" for the rain queue
	_em = update.effective_message
	if _em.text and _em.entities:
		_e0 = _em.entities[0]
		if getattr(_e0, "offset", None) == 0:
			_et = _e0.type
			if _et == "bot_command" or getattr(_et, "name", None) == "BOT_COMMAND":
				return
	# Get user_id for the tip command (either @username or else UserID)
	_username = update.effective_user.username
	_user_id = str(update.effective_user.id)  # The queue uses real UserID to avoid registering a user twice if user creates @
	if _username is None:
		_user_id_local = _user_id
		_user_readable_name = update.effective_user.name
	else:
		_user_id_local = '@' + _username.lower()
		_user_readable_name = _username
	if update.effective_message.text is not None:
		if len(update.effective_message.text) < __rain_queue_min_text_length:
			return
		if len(update.effective_message.text.split()) < __rain_queue_min_words:
			return
	# Check the queue
	if _group_id not in _rain_queues:
		_rain_queues[_group_id] = []
	# Note: In Python2, Dict doesn't preserve order as in Python3 (see https://stackoverflow.com/questions/14956313)
	if len(_rain_queues[_group_id]) > 0:
		# If user has talked last, don't remove it, don't add it.
		if _rain_queues[_group_id][0][0] == _user_id:  # Don't use "is not", the object will not be the same, only value will
			return
		else:
			# Search for user and remove it (in order to place it first)
			for _user_data in _rain_queues[_group_id]:
				if _user_data[0] == _user_id:
					_rain_queues[_group_id].remove(_user_data)
					break
	# Add user to queue (first, since it will be read from first to last)
	_rain_queues[_group_id].insert(0, (_user_id, _user_id_local, _user_readable_name))
	# Check if the queue has to be pruned
	if len(_rain_queues[_group_id]) > __rain_queue_max_members:
		_rain_queues[_group_id].pop()  # pop(-1). This should be enough to remove the last member, but real pruning would be better
	_debug_log.debug("damp_rock queue[%s] len=%s", _group_id, len(_rain_queues[_group_id]))


def rain(update, context):
	"""
	/rain <wjk_each> [how_many_people]
	Each of up to how_many_people recently active members (excl. sender) receives wjk_each WJK.
	Omit how_many_people to include everyone eligible (capped at the configured queue limit).
	Total spent = wjk_each * (actual recipient count).
	"""
	args = context.args or []
	_debug_log.debug("rain user=%s group=%s args=%s", update.effective_user.id, getattr(update.effective_chat, "id", None), args)
	if not _spam_filter.verify(str(update.effective_user.id)):
		return
	if _paused:
		update.message.reply_text(text=emoji.emojize(strings.get("global_paused"), language='alias'), quote=True)
		return
	if update.effective_chat is None:
		return
	elif update.effective_chat.type not in ["group", "supergroup"]:
		return
	#
	_group_id = str(update.effective_chat.id)
	_user_id = str(update.effective_user.id)
	if len(args) == 0 or len(args) > 2:
		update.message.reply_text(
			"Use `/rain <wjk_each> [how_many]` — each of up to `how_many` *active* members gets `wjk_each` WJK (you are excluded). "
			"Omit `how_many` to rain on everyone eligible (up to %i)." % __rain_queue_max_members,
			quote=True,
			parse_mode=ParseMode.MARKDOWN
		)
		return
	if 0 < len(args) <= 2:  # We may or may not allow text after the first 2 arguments. Probably not.
		# Check if queue has enough members
		if _group_id not in _rain_queues:
			update.message.reply_text(
				strings.get("rain_queue_not_initialized", _lang),
				quote=True,
				parse_mode=ParseMode.MARKDOWN,
				disable_web_page_preview=True
			)
			return
		# Prepare arguments
		_amount_each = 0
		_rain_members_demanded = __rain_queue_max_members  # cap on recipients when how_many omitted
		try:
			_amount_each = int(args[0])
			if len(args) > 1:
				_rain_members_demanded = int(args[1])
		except ValueError:
			return  # Don't show error. Probably trolling.
		if len(args) > 1 and (_rain_members_demanded < __rain_min_members or _rain_members_demanded > __rain_queue_max_members):
			update.message.reply_text(
				strings.get("rain_queue_min_max_members", _lang) % (__rain_min_members, __rain_queue_max_members, _rain_members_demanded),
				quote=True,
				parse_mode=ParseMode.MARKDOWN,
				disable_web_page_preview=True
			)
			return
		# Check if user is in queue, don't remove user from original queue as recipients array will be created later
		# Note that using this command doesn't put the user in queue (commands are excluded from damp_rock())
		_modifier = 0
		for _user_data in _rain_queues[_group_id]:
			if _user_data[0] == _user_id:
				_modifier = -1
				break
		# Check if there are enough members in queue (minus user if needed)
		if len(_rain_queues[_group_id]) + _modifier < __rain_min_members:
			update.message.reply_text(
				strings.get("rain_queue_not_enough_members", _lang) % (
					len(_rain_queues[_group_id]) + _modifier,
					- _modifier,
					__rain_min_members
				),
				quote=True,
				parse_mode=ParseMode.MARKDOWN,
				disable_web_page_preview=True
			)
			return
		# Build recipients list (up to _rain_members_demanded, excluding sender)
		_recipients = []  # Array of LocalUserID
		_handled = {}  # Dict of LocalUserID: (Readable Name, Unused, Unused)
		for _user_data in _rain_queues[_group_id]:
			if _user_data[0] != _user_id:
				_recipients.append(_user_data[1])
				_handled[_user_data[1]] = (_user_data[2], None, None)
				if len(_recipients) >= _rain_members_demanded:
					break
		n_recipients = len(_recipients)
		if n_recipients == 0:
			update.message.reply_text(
				strings.get("rain_queue_not_initialized", _lang),
				quote=True,
				parse_mode=ParseMode.MARKDOWN,
				disable_web_page_preview=True
			)
			return
		if _amount_each < __rain_min_amount:
			update.message.reply_text(
				strings.get("rain_queue_min_amount", _lang) % (__rain_min_amount, "WJK", _amount_each, "WJK"),
				quote=True,
				parse_mode=ParseMode.MARKDOWN,
				disable_web_page_preview=True
			)
			return
		_total_out = _amount_each * n_recipients
		_debug_log.debug("rain each=%s n_recipients=%s total=%s recipients=%s", _amount_each, n_recipients, _total_out, _recipients)
		log("rain", _user_id, "rain %i WJK each x %i active members (total %i) handed to do_tip()" % (_amount_each, n_recipients, _total_out))
		do_tip(update, context, [_amount_each], _recipients, _handled, verb="rain")


def do_tip(update, context, amounts_float, recipients, handled, verb="tip"):
	bot = context.bot
	"""
	Send amounts to recipients
	:param bot: Bot
	:param update: Update
	:param amounts_float: Array of Float
	:param recipients: Array of Username or UserID
	:param handled: Dict of {"username or UserID": (username, entity.offset, entity.length)
	:param verb: "tip", will be used in "%verb%_success" and "%verb%_missing_recipient" strings
	:return: None
	"""
	#
	_debug_log.debug("do_tip verb=%s len(amounts)=%s len(recipients)=%s amounts=%s recipients=%s", verb, len(amounts_float), len(recipients), amounts_float, recipients)
	if verb not in ["tip", "rain"]:
		log("do_tip", "__system__", "Incorrect verb passed to do_tip()")
		verb = "tip"
	# Check if only 1 amount is given
	_amounts_float = amounts_float
	if len(_amounts_float) == 1 and len(recipients) > 1:
		_amounts_float = _amounts_float * len(recipients)
	# Check if user has enough balance
	_username = update.effective_user.username
	if _username is None:
		_user_id = str(update.effective_user.id)
	else:
		_user_id = '@' + _username.lower()
	# get address of user (used as primary/change address for raw tx)
	_rpc_call = __wallet_rpc.getaddressesbyaccount(_user_id)
	if not _rpc_call["success"]:
		print("Error during RPC call: %s" % _rpc_call["message"])
		log("do_tip", _user_id, "(1) getaddressesbyaccount > Error during RPC call: %s" % _rpc_call["message"])
	elif _rpc_call["result"]["error"] is not None:
		print("Error: %s" % _rpc_call["result"]["error"])
		log("do_tip", _user_id, "(1) getaddressesbyaccount > Error: %s" % _rpc_call["result"]["error"])
	else:
		_addresses = _rpc_call["result"]["result"]
		if len(_addresses) == 0:
			# User has no address, ask him to create one
			msg_no_account(update, context)
			return
		_from_address = _addresses[0]
		# Get user's balance using the account name (not the address)
		_rpc_call = __wallet_rpc.getbalance(_user_id, 0)
		if not _rpc_call["success"]:
			print("Error during RPC call.")
			log("do_tip", _user_id, "(2) getbalance > Error during RPC call: %s" % _rpc_call["message"])
		elif _rpc_call["result"]["error"] is not None:
			print("Error: %s" % _rpc_call["result"]["error"])
			log("do_tip", _user_id, "(2) getbalance > Error: %s" % _rpc_call["result"]["error"])
		else:
			_balance = int(_rpc_call["result"]["result"])
			_debug_log.debug("do_tip balance=%s sum(amounts)=%s recipients_count=%s", _balance, sum(_amounts_float), len(recipients))
			# Now, finally, check if user has enough funds (includes tx fee)
			if sum(_amounts_float) > _balance - max(1, int(len(recipients)/3)):
				update.message.reply_text(
					text="%s `%i WJK`" % (strings.get("tip_no_funds", _lang), sum(_amounts_float) + max(1, int(len(recipients)/3))),
					quote=True,
					parse_mode=ParseMode.MARKDOWN
				)
			else:
				# Now create the {recipient_id: amount} dictionary for display
				# and {address: amount} dictionary for RPC sendmany
				i = 0
				_tip_dict = {}
				send_dict = {}
				for _recipient in recipients:
					# add "or _recipient == bot.id" to disallow tipping the tip bot
					if _recipient == _user_id:
						i += 1
						continue
					if _recipient[0] == '@':
						# ToDo: Get the id (actually not possible (Bot API 3.6, Feb. 2018)
						# See issue #2 (https://github.com/DarthJahus/PandaTip-Telegram/issues/2)
						# Using the @username
						# Done: When requesting a new address, if user has a @username, then use that username (2018-07-16)
						# Problem: If someone has no username, then later creates one, he loses access to his account
						# Done: Create a /scavenge command that allows people who had UserID to migrate to UserName (2018-07-16)
						_recipient_id = _recipient.lower()  # Enforce lowercase
					else:
						_recipient_id = _recipient
					# Check if recipient has an address (required for .sendmany()
					_rpc_call = __wallet_rpc.getaddressesbyaccount(_recipient_id)
					if not _rpc_call["success"]:
						print("Error during RPC call.")
						log("do_tip", _user_id, "(3) getaddressesbyaccount(%s) > Error during RPC call: %s" % (_recipient_id, _rpc_call["message"]))
					elif _rpc_call["result"]["error"] is not None:
						print("Error: %s" % _rpc_call["result"]["error"])
						log("do_tip", _user_id, "(3) getaddressesbyaccount(%s) > Error: %s" % (_recipient_id, _rpc_call["result"]["error"]))
					else:
						_address = None
						_addresses = _rpc_call["result"]["result"]
						if len(_addresses) == 0:
							# Recipient has no address, create one
							_rpc_call = __wallet_rpc.getaccountaddress(_recipient_id)
							if not _rpc_call["success"]:
								print("Error during RPC call.")
								log("do_tip", _user_id, "(4) getaccountaddress(%s) > Error during RPC call: %s" % (_recipient_id, _rpc_call["message"]))
							elif _rpc_call["result"]["error"] is not None:
								print("Error: %s" % _rpc_call["result"]["error"])
								log("do_tip", _user_id, "(4) getaccountaddress(%s) > Error: %s" % (_recipient_id, _rpc_call["result"]["error"]))
							else:
								_address = _rpc_call["result"]["result"]
						else:
							# Recipient has an address, we don't need to create one for him
							_address = _addresses[0]
					if _address is not None:
						# Because recipient has an address, we can add him to both dicts
						_tip_dict[_recipient_id] = _amounts_float[i]
						# WojakCoin sendmany expects addresses as keys
						send_dict[_address] = _amounts_float[i]
					i += 1
				# After building send_dict: one tx per recipient (do not send inside the loop above)
				_debug_log.debug("do_tip send_dict=%s _tip_dict=%s", send_dict, _tip_dict)
				if len(_tip_dict) == 0:
					return
				fee = 0.0001  # fixed fee, matches wallet paytxfee
				txids = []
				for _address, _amount in send_dict.items():
					# 1) Select UTXOs from the sender's address (minconf=0, spend unconfirmed)
					_rpc_call = __wallet_rpc.listunspent(0, 9999999, [_from_address])
					if not _rpc_call["success"]:
						print("Error during RPC call.")
						log("do_tip", _user_id, "(4) listunspent(%s) > Error during RPC call: %s" % (_from_address, _rpc_call["message"]))
						return
					elif _rpc_call["result"]["error"] is not None:
						print("Error: %s" % _rpc_call["result"]["error"])
						log("do_tip", _user_id, "(4) listunspent(%s) > Error: %s" % (_from_address, _rpc_call["result"]["error"]))
						return

					_utxos = _rpc_call["result"]["result"]
					inputs = []
					total_in = 0.0
					for utxo in _utxos:
						inputs.append({
							"txid": utxo["txid"],
							"vout": utxo["vout"],
						})
						total_in += float(utxo["amount"])
						if total_in >= _amount + fee:
							break

					if total_in < _amount + fee:
						update.message.reply_text(
							text="%s `%i WJK`" % (strings.get("tip_no_funds", _lang), _amount),
							quote=True,
							parse_mode=ParseMode.MARKDOWN
						)
						log("do_tip", _user_id, "(4) listunspent > Not enough UTXOs for raw tip")
						return

					change = total_in - _amount - fee
					outputs = {
						_address: float(_amount)
					}
					# Only create a change output if change is positive and non-dust
					if change > 0:
						outputs[_from_address] = round(change, 8)

					# 2) Create raw transaction
					_rpc_call = __wallet_rpc.createrawtransaction(inputs, outputs)
					if not _rpc_call["success"]:
						print("Error during RPC call.")
						log("do_tip", _user_id, "(4) createrawtransaction > Error during RPC call: %s" % _rpc_call["message"])
						return
					elif _rpc_call["result"]["error"] is not None:
						print("Error: %s" % _rpc_call["result"]["error"])
						log("do_tip", _user_id, "(4) createrawtransaction > Error: %s" % _rpc_call["result"]["error"])
						return

					rawtx = _rpc_call["result"]["result"]

					# 3) Sign raw transaction
					_rpc_call = __wallet_rpc.signrawtransaction(rawtx)
					if not _rpc_call["success"]:
						print("Error during RPC call.")
						log("do_tip", _user_id, "(4) signrawtransaction > Error during RPC call: %s" % _rpc_call["message"])
						return
					elif _rpc_call["result"]["error"] is not None:
						print("Error: %s" % _rpc_call["result"]["error"])
						log("do_tip", _user_id, "(4) signrawtransaction > Error: %s" % _rpc_call["result"]["error"])
						return

					_sign_res = _rpc_call["result"]["result"]
					if not _sign_res.get("complete", False):
						log("do_tip", _user_id, "(4) signrawtransaction > Incomplete signature")
						return

					signed_hex = _sign_res["hex"]

					# 4) Broadcast raw transaction
					_rpc_call = __wallet_rpc.sendrawtransaction(signed_hex)
					if not _rpc_call["success"]:
						print("Error during RPC call.")
						log("do_tip", _user_id, "(4) sendrawtransaction > Error during RPC call: %s" % _rpc_call["message"])
						return
					elif _rpc_call["result"]["error"] is not None:
						print("Error: %s" % _rpc_call["result"]["error"])
						log("do_tip", _user_id, "(4) sendrawtransaction > Error: %s" % _rpc_call["result"]["error"])
						return

					txids.append(_rpc_call["result"]["result"])
					_debug_log.debug("do_tip sent tx %s -> %s amount=%s", _rpc_call["result"]["result"][:12], _address[:12], _amount)

				_suppl = ""
				if len(_tip_dict) != len(recipients):
					_suppl = "\n\n_%s_" % strings.get("%s_missing_recipient" % verb, _lang)

				# Use the first txid for the explorer link if there are multiple
				_tx = txids[0] if txids else ""
				tx_link = ""
				if _tx != "":
					tx_link = "[tx %s](%s)" % (
						_tx[:4] + "..." + _tx[-4:],
						"https://explorer.wojakcoin2017.xyz/tx/" + _tx
					)

				update.message.reply_text(
					text = "*%s* %s\n%s\n\n%s%s" % (
						update.effective_user.name,
						strings.get("%s_success" % verb, _lang),
						''.join((("\n- `%3.0f WJK ` %s *%s*" % (_tip_dict[_recipient_id], strings.get("%s_preposition" % verb, _lang), handled[_recipient_id][0])) for _recipient_id in _tip_dict)),
						tx_link,
						_suppl
					),
					quote=True,
					parse_mode=ParseMode.MARKDOWN,
					disable_web_page_preview=True
				)


# Done: Revamp withdraw() function (2018-07-16)
def withdraw(update, context):
	"""
	Withdraw to an address. Works only in private.
	"""
	args = context.args or []
	_debug_log.debug("withdraw user=%s args=%s", update.effective_user.id, args)
	if update.effective_chat is None:
		_chat_type = "private"
	elif update.effective_chat.type == "private":
		_chat_type = "private"
	else:
		_chat_type = "group"
	#
	if _chat_type == "private":
		if not _spam_filter.verify(str(update.effective_user.id)):
			return
		if _paused:
			update.message.reply_text(text=emoji.emojize(strings.get("global_paused"), language='alias'), quote=True)
			return
		_amount = None
		_recipient = None
		if len(args) == 2:
			try:
				_amount = int(args[1])
				_recipient = args[0]
			except:
				try:
					_amount = int(args[0])
					_recipient = args[1]
				except:
					pass
		else:
			update.message.reply_text(
				text="Too few or too many arguments for this command.",
				quote=True
			)
		if _amount is not None and _recipient is not None:
			_username = update.effective_user.username
			if _username is None:
				_user_id = str(update.effective_user.id)
			else:
				_user_id = '@' + _username.lower()
			# get address of user
			_rpc_call = __wallet_rpc.getaddressesbyaccount(_user_id)
			if not _rpc_call["success"]:
				print("Error during RPC call: %s" % _rpc_call["message"])
				log("withdraw", _user_id, "(1) getaddressesbyaccount > Error during RPC call: %s" % _rpc_call["message"])
			elif _rpc_call["result"]["error"] is not None:
				print("Error: %s" % _rpc_call["result"]["error"])
				log("withdraw", _user_id, "(1) getaddressesbyaccount > Error: %s" % _rpc_call["result"]["error"])
			else:
				_addresses = _rpc_call["result"]["result"]
				if len(_addresses) == 0:
					# User has no address, ask him to create one
					msg_no_account(update, context)
				else:
					_address = _addresses[0]
					_rpc_call = __wallet_rpc.getbalance(_address)
					if not _rpc_call["success"]:
						print("Error during RPC call.")
						log("withdraw", _user_id, "(2) getbalance > Error during RPC call: %s" % _rpc_call["message"])
					elif _rpc_call["result"]["error"] is not None:
						print("Error: %s" % _rpc_call["result"]["error"])
						log("withdraw", _user_id, "(2) getbalance > Error: %s" % _rpc_call["result"]["error"])
					else:
						_balance = int(_rpc_call["result"]["result"])
						if _balance < _amount + 5:
							update.message.reply_text(
								text="%s `%i WJK`" % (strings.get("withdraw_no_funds", _lang), _balance-5),
								quote=True,
								parse_mode=ParseMode.MARKDOWN
							)
						else:
							# Withdraw
							_rpc_call = __wallet_rpc.sendfrom(_user_id, _recipient, _amount)
							if not _rpc_call["success"]:
								print("Error during RPC call.")
								log("withdraw", _user_id, "(3) sendfrom > Error during RPC call: %s" % _rpc_call["message"])
							elif _rpc_call["result"]["error"] is not None:
								print("Error: %s" % _rpc_call["result"]["error"])
								log("withdraw", _user_id, "(3) sendfrom > Error: %s" % _rpc_call["result"]["error"])
							else:
								_tx = _rpc_call["result"]["result"]
								update.message.reply_text(
									text="%s\n[tx %s](%s)" % (
										strings.get("withdraw_success", _lang),
										_tx[:4]+"..."+_tx[-4:],
										"https://explorer.wojakcoin2017.xyz/tx/" + _tx
									),
									quote=True,
									parse_mode=ParseMode.MARKDOWN,
									disable_web_page_preview=True
								)


def scavenge(update, context):
	_debug_log.debug("scavenge user=%s", update.effective_user.id)
	if update.effective_chat is None:
		_chat_type = "private"
	elif update.effective_chat.type == "private":
		_chat_type = "private"
	else:
		_chat_type = "group"
	# Only if it's a private conversation with the bot
	if _chat_type == "private":
		if not _spam_filter.verify(str(update.effective_user.id)):
			return
		if _paused:
			update.message.reply_text(text=emoji.emojize(strings.get("global_paused"), language='alias'), quote=True)
			return
		_username = update.effective_user.username
		if _username is None:
			update.message.reply_text(
				text="Sorry, this command is not for you.",
				quote=True
			)
		else:
			_username = '@' + _username.lower()
			_user_id = str(update.effective_user.id)
			# Done: Check balance of UserID (2018-07-16)
			# get address of user
			_rpc_call = __wallet_rpc.getaddressesbyaccount(_user_id)
			if not _rpc_call["success"]:
				print("Error during RPC call: %s" % _rpc_call["message"])
				log("scavenge", _user_id, "(1) getaddressesbyaccount > Error during RPC call: %s" % _rpc_call["message"])
			elif _rpc_call["result"]["error"] is not None:
				print("Error: %s" % _rpc_call["result"]["error"])
				log("scavenge", _user_id, "(1) getaddressesbyaccount > Error: %s" % _rpc_call["result"]["error"])
			else:
				_addresses = _rpc_call["result"]["result"]
				if len(_addresses) == 0:
					update.message.reply_text(
						text="%s (`%s`)" % (strings.get("scavenge_no_address", _lang), _user_id),
						quote=True,
					)
				else:
					_address = _addresses[0]
					_rpc_call = __wallet_rpc.getbalance(_address)
					if not _rpc_call["success"]:
						print("Error during RPC call.")
						log("scavenge", _user_id, "(2) getbalance > Error during RPC call: %s" % _rpc_call["message"])
					elif _rpc_call["result"]["error"] is not None:
						print("Error: %s" % _rpc_call["result"]["error"])
						log("scavenge", _user_id, "(2) getbalance > Error: %s" % _rpc_call["result"]["error"])
					else:
						_balance = int(_rpc_call["result"]["result"])
						# Done: Move balance from UserID to @username if balance > 5 (2018-07-16)
						if _balance <= 5:
							update.message.reply_text(
								text="%s (`ID %s`)." % (strings.get("scavenge_empty", _lang), _user_id),
								parse_mode=ParseMode.MARKDOWN,
								quote=True
							)
						else:
							# Need to make sure there is an account for _username
							_rpc_call = __wallet_rpc.getaddressesbyaccount(_username)
							if not _rpc_call["success"]:
								print("Error during RPC call: %s" % _rpc_call["message"])
								log("scavenge", _user_id, "(3) getaddressesbyaccount > Error during RPC call: %s" % _rpc_call["message"])
							elif _rpc_call["result"]["error"] is not None:
								print("Error: %s" % _rpc_call["result"]["error"])
								log("scavenge", _user_id, "(3) getaddressesbyaccount > Error: %s" % _rpc_call["result"]["error"])
							else:
								_address = None
								_addresses = _rpc_call["result"]["result"]
								if len(_addresses) == 0:
									# Create an address for user (_username)
									_rpc_call = __wallet_rpc.getaccountaddress(_username)
									if not _rpc_call["success"]:
										print("Error during RPC call.")
										log("scavenge", _user_id, "(4) getaccountaddress > Error during RPC call: %s" % _rpc_call["message"])
									elif _rpc_call["result"]["error"] is not None:
										print("Error: %s" % _rpc_call["result"]["error"])
										log("scavenge", _user_id, "(4) getaccountaddress > Error: %s" % _rpc_call["result"]["error"])
									else:
										_address = _rpc_call["result"]["result"]
								else:
									_address = _addresses[0]
								if _address is not None:
									# Move the funds from UserID to Username
									# ToDo: Make the fees consistent
									_rpc_call = __wallet_rpc.sendfrom(_user_id, _address, _balance-5)
									if not _rpc_call["success"]:
										print("Error during RPC call.")
										log("scavenge", _user_id, "(5) sendfrom > Error during RPC call: %s" % _rpc_call["message"])
									elif _rpc_call["result"]["error"] is not None:
										print("Error: %s" % _rpc_call["result"]["error"])
										log("scavenge", _user_id, "(5) sendfrom > Error: %s" % _rpc_call["result"]["error"])
									else:
										_tx = _rpc_call["result"]["result"]
										update.message.reply_text(
											text="%s (`%s`).\n%s `%i WJK`\n[tx %s](%s)" % (
												strings.get("scavenge_success_1", _lang),
												_user_id,
												strings.get("scavenge_success_2", _lang),
												_balance-5,
												_tx[:4]+"..."+_tx[-4:],
												"https://explorer.wojakcoin2017.xyz/tx/" + _tx,
											),
											quote=True,
											parse_mode=ParseMode.MARKDOWN,
											disable_web_page_preview=True
										)


def convert_to_int(text):
	# with panda feature :D (2018-07-18)
	try:
		# try convert to float
		return int(text)
	except:
		# Check if the text is made of pandas
		if len(text)/2 > 3 or len(text) == 0 or len(text) % 2 != 0:
			raise ValueError("Can't convert %s to int." % text)
		else:
			_panda = emoji.emojize(":panda_face:", language='alias')
			_debug_log.debug("convert_to_int panda len=%s", len(text))
			for i in range(len(text)):
				if text[i] != _panda[i%2]:
					raise ValueError("Can't convert %s to int." % text)
			else:
				return 10**(int(len(text)/2))


def cmd_send_log(update, context):
	"""
	Send logs to (admin) user
	"""
	_debug_log.debug("cmd_send_log user=%s chat_id=%s", update.effective_user.id, update.effective_chat.id)
	bot = context.bot
	# Note: Don't use emoji in caption
	# Check if admin
	if update.effective_chat.id in config["admins"]:
		with open("log.csv", "rb") as _file:
			_file_name = "%s-log-%s.csv" % (bot.username, datetime.fromtimestamp(time.time()).strftime("%Y-%m-%dT%H-%M-%S"))
			bot.sendDocument(
				chat_id=update.effective_user.id,
				document=_file,
				reply_to_message_id=update.message.message_id,
				caption="Here you are!",
				filename=_file_name
			)
		log(fun="cmd_send_log", user=str(update.effective_user.id), message="Log sent to admin '%s'." % update.effective_user.name)


def cmd_clear_log(update, context):
	_debug_log.debug("cmd_clear_log user=%s chat_id=%s", update.effective_user.id, getattr(update.effective_chat, "id", None))
	if update.effective_chat in config["admins"]:
		clear_log()
		update.message.reply_text(text=emoji.emojize(strings.get("clear_log_done"), language='alias'))


def cmd_pause(update, context):
	_debug_log.debug("cmd_pause user=%s chat_id=%s", update.effective_user.id, update.effective_chat.id)
	# Admins only
	if update.effective_chat.id in config["admins"]:
		global _paused
		_paused = not _paused
		_answer = ""
		if _paused:
			_answer = strings.get("pause_answer_paused")
		else:
			_answer = strings.get("pause_answer_resumed")
		update.message.reply_text(emoji.emojize(_answer, language='alias'), quote=True)
		# Reinitialize rain queues
		_rain_queues.clear()


def error_handler(update, context):
	"""Handle errors in the telegram bot (python-telegram-bot 13.x style)."""
	error = context.error
	_debug_log.debug("error_handler update=%s error=%s", update, error)
	if isinstance(error, NetworkError):
		_debug_log.warning("Network error occurred: %s. Retrying...", error)
		time.sleep(2)  # Wait before retry
		return
	elif isinstance(error, TimedOut):
		_debug_log.warning("Request timed out: %s. Retrying...", error)
		time.sleep(1)
		return
	elif isinstance(error, RetryAfter):
		_debug_log.warning("Rate limited. Retrying after %s seconds...", getattr(error, 'retry_after', 1))
		time.sleep(getattr(error, 'retry_after', 1))
		return
	else:
		_debug_log.error("Update %s caused error %s", update, error, exc_info=True)


if __name__ == "__main__":
	_debug_log.debug("__main__ starting debug=%s config_keys=%s", config.get("debug"), list(config.keys()))
	# Create updater with request timeout settings
	updater = Updater(
		token=config["telegram-token"],
		request_kwargs={
			'read_timeout': 30,
			'connect_timeout': 30
		}
	)
	dispatcher = updater.dispatcher
	_debug_log.debug("__main__ handlers: start help about links buy wallets price mining tip withdraw deposit balance scavenge rain checkactive send_log clear_log pause MessageHandler(damp_rock)")
	# Add error handler
	dispatcher.add_error_handler(error_handler)
	# TGBot commands
	dispatcher.add_handler(CommandHandler("start", cmd_start, pass_args=True))
	dispatcher.add_handler(CommandHandler("help", cmd_help))
	dispatcher.add_handler(CallbackQueryHandler(callback=cmd_help, pattern=r'^help$'))
	dispatcher.add_handler(CommandHandler("about", cmd_about))
	dispatcher.add_handler(CallbackQueryHandler(callback=cmd_about, pattern=r'^about$'))
	dispatcher.add_handler(CommandHandler("links", cmd_links))
	dispatcher.add_handler(CommandHandler("buy", cmd_buy))
	dispatcher.add_handler(CommandHandler("wallets", cmd_wallets))
	dispatcher.add_handler(CommandHandler("price", cmd_price))
	dispatcher.add_handler(CommandHandler("mining", cmd_mining))
	# Tipbot commands
	dispatcher.add_handler(CommandHandler("tip", tip, pass_args=True))
	dispatcher.add_handler(CommandHandler("withdraw", withdraw, pass_args=True))
	dispatcher.add_handler(CommandHandler("deposit", deposit))
	dispatcher.add_handler(CommandHandler("address", deposit)) # alias for /deposit
	dispatcher.add_handler(CommandHandler("balance", balance))
	dispatcher.add_handler(CommandHandler("scavenge", scavenge))
	dispatcher.add_handler(CommandHandler("rain", rain, pass_args=True))
	dispatcher.add_handler(CommandHandler("checkactive", check_active))
	# Admin commands
	dispatcher.add_handler(CommandHandler("send_log", cmd_send_log))
	dispatcher.add_handler(CommandHandler("get_log", cmd_send_log))
	dispatcher.add_handler(CommandHandler("clear_log", cmd_clear_log))
	dispatcher.add_handler(CommandHandler("pause", cmd_pause)) # pause / unpause
	# This will be needed for rain
	dispatcher.add_handler(MessageHandler(__rain_queue_filter, damp_rock))
	#
	# Block announce: send new block details to group when configured
	block_chat_id = config.get("block_announce_chat_id")
	_debug_log.debug("__main__ block_announce_chat_id=%s", block_chat_id)
	if block_chat_id is not None:
		_block_thread = threading.Thread(target=_block_announce_loop, args=(updater, block_chat_id), daemon=True)
		_block_thread.start()
		log("__main__", "__system__", "Block announce enabled for chat_id %s" % block_chat_id)
	#
	# Start polling with error handling and retry logic
	log("__main__", "__system__", "Starting service...")
	_debug_log.debug("__main__ start_polling")
	try:
		updater.start_polling(
			poll_interval=1.0,
			timeout=30,
			bootstrap_retries=-1,  # Retry indefinitely on startup
			read_latency=2.0,
			clean=False  # Don't clear pending updates on startup
		)
		log("__main__", "__system__", "Started service!")
		# Keep the bot running (start_polling blocks in v12)
		updater.idle()
	except KeyboardInterrupt:
		log("__main__", "__system__", "Received interrupt signal. Shutting down...")
		_block_announce_stop = True
		updater.stop()
	except Exception as e:
		log("__main__", "__system__", f"Fatal error: {e}")
		logging.error(f"Fatal error in main: {e}", exc_info=True)
		_block_announce_stop = True
		updater.stop()
		raise
