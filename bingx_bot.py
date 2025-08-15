import os
import asyncio
import json
import time
from typing import Dict, Any, List, Optional, Tuple

import aiohttp
import logging

# Конфигурация логирования
LOG_LEVEL_STR = os.getenv('LOG_LEVEL', 'INFO').upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_STR, logging.INFO)
logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# ------------------ Config ------------------
def read_api_credentials(file_path: str = 'api.txt') -> Tuple[Optional[str], Optional[str]]:
    """Читает API ключ и секрет из файла api.txt.

    Формат строк:
    - API_KEY=...
    - SECRET_KEY=...  или  API_SECRET=...
    """
    try:
        if not os.path.exists(file_path):
            logger.warning("Файл с ключами не найден: %s", file_path)
            return None, None
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        api_key = None
        api_secret = None
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('API_KEY='):
                api_key = line.split('=', 1)[1].strip()
            elif line.startswith('SECRET_KEY=') or line.startswith('API_SECRET='):
                api_secret = line.split('=', 1)[1].strip()
        if not api_key or not api_secret:
            logger.warning("API_KEY или API_SECRET отсутствуют в %s", file_path)
        return api_key, api_secret
    except Exception as e:
        logger.error("Ошибка чтения %s: %s", file_path, e)
        return None, None

# Новая функция: читаем Telegram токен/чат из api.txt
def read_telegram_credentials(file_path: str = 'api.txt') -> Tuple[Optional[str], Optional[str]]:
    try:
        if not os.path.exists(file_path):
            return None, None
        token = None
        chat_id = None
        with open(file_path, 'r', encoding='utf-8') as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith('#'):
                    continue
                if line.startswith('TG_BOT_TOKEN='):
                    token = line.split('=', 1)[1].strip()
                elif line.startswith('TG_CHAT_ID='):
                    chat_id = line.split('=', 1)[1].strip()
        return token, chat_id
    except Exception as e:
        logger.error("Ошибка чтения %s: %s", file_path, e)
        return None, None

SYMBOL = os.getenv('SYMBOL', 'BTC-USDT')
INTERVAL = os.getenv('INTERVAL', '15m')
USE_TESTNET = os.getenv('USE_TESTNET', 'false').lower() in ('1', 'true', 'yes')
API_KEY, API_SECRET = read_api_credentials()

# Telegram notif config: сначала ENV, затем fallback к api.txt
TG_BOT_TOKEN_ENV = os.getenv('TG_BOT_TOKEN')
TG_CHAT_ID_ENV = os.getenv('TG_CHAT_ID')
TG_FILE_TOKEN, TG_FILE_CHAT = read_telegram_credentials()
TG_BOT_TOKEN = TG_BOT_TOKEN_ENV or TG_FILE_TOKEN
TG_CHAT_ID = TG_CHAT_ID_ENV or TG_FILE_CHAT

# BingX V2 endpoints
WS_REST_PUBLIC = 'wss://open-api-ws.bingx.com/market'
REST_BASE = 'https://open-api.bingx.com'

# Parameters to tune
MAX_HISTORY = 1500  # кол-во свечей текущего ТФ
MIN_HISTORY_FOR_SIGNAL = 120  # минимум свечей на ТФ для сигналов
ATR_PERIOD = 14
MIN_RR = 2.0
COOLDOWN_SECS = 90  # кулдаун между сигналами

# SMC-like parameters (mapping from Pine)
SWINGS_LENGTH = int(os.getenv('SWINGS_LENGTH', '50'))  # аналог swingsLengthInput
INTERNAL_LENGTH = int(os.getenv('INTERNAL_LENGTH', '5'))  # аналог внутренней структуры
EQUAL_LENGTH = int(os.getenv('EQUAL_LENGTH', '3'))
EQUAL_THRESHOLD = float(os.getenv('EQUAL_THRESHOLD', '0.1'))  # как доля ATR(200)

# ------------------ Telegram ------------------
class TelegramNotifier:
    def __init__(self, token: Optional[str], chat_id: Optional[str]):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}" if token else None

    async def send(self, text: str) -> bool:
        if not self.base_url or not self.chat_id:
            logger.debug('Telegram send skipped: missing token/chat_id')
            return False
        url = f"{self.base_url}/sendMessage"
        payload = {
            'chat_id': self.chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        t = await resp.text()
                        logger.error('Telegram send failed [%s]: %s', resp.status, t)
                        return False
            logger.debug('Telegram message sent')
            return True
        except Exception as e:
            logger.error('Telegram error: %s', e)
            return False

# ------------------ Market Data ------------------
class MarketData:
    def __init__(self, symbol: str, interval: str):
        self.symbol = symbol
        self.interval = interval
        self.candles: List[Dict[str, Any]] = []  # текущий ТФ
        self.one_hour_candles: List[Dict[str, Any]] = []
        self.four_hour_candles: List[Dict[str, Any]] = []
        self.daily_candles: List[Dict[str, Any]] = []
        self.weekly_candles: List[Dict[str, Any]] = []
        self.orderbook_top = {'bids': [], 'asks': []}
        self.ws = None
        self._last_kline_ts: Optional[int] = None

    async def _rest_get(self, url: str, params: Dict[str, str]) -> Optional[Dict[str, Any]]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    text = await resp.text()
                    logger.error("REST %s failed [%s]: %s", url, resp.status, text)
        except Exception as e:
            logger.error("REST %s error: %s", url, e)
        return None

    async def fetch_recent_candles(self, interval: str, limit: int = 150) -> List[Dict[str, Any]]:
        path = '/openApi/market/kline'
        url = f"{REST_BASE}{path}"
        params = {'symbol': self.symbol, 'interval': interval, 'limit': str(limit)}
        data = await self._rest_get(url, params)
        candles: List[Dict[str, Any]] = []
        if data and isinstance(data.get('data'), list):
            for c in data['data']:
                try:
                    ts = int(c[0])
                    candles.append({
                        'timestamp': ts,
                        'open': float(c[1]),
                        'high': float(c[2]),
                        'low': float(c[3]),
                        'close': float(c[4]),
                        'volume': float(c[5])
                    })
                except Exception:
                    continue
        return candles

    async def seed_current_interval(self, limit: int = 300) -> None:
        candles = await self.fetch_recent_candles(self.interval, limit)
        if candles:
            self.candles = candles[-MAX_HISTORY:]
            self._last_kline_ts = self.candles[-1]['timestamp']
            logger.info("Инициализировано %s свечей для %s %s", len(self.candles), self.symbol, self.interval)
        else:
            logger.warning("Не удалось инициализировать свечи для %s %s", self.symbol, self.interval)

    async def fetch_one_hour_candles(self):
        self.one_hour_candles = await self.fetch_recent_candles('1h', 200)

    async def fetch_four_hour_candles(self):
        self.four_hour_candles = await self.fetch_recent_candles('4h', 200)

    async def fetch_daily_candles(self):
        self.daily_candles = await self.fetch_recent_candles('1d', 200)

    async def fetch_weekly_candles(self):
        self.weekly_candles = await self.fetch_recent_candles('1w', 200)

    async def connect_ws(self):
        url = WS_REST_PUBLIC
        max_retries = 10
        retry_delay = 3
        for attempt in range(max_retries):
            try:
                logger.info('Подключение к WS %s для %s %s (попытка %s/%s)', url, self.symbol, self.interval, attempt + 1, max_retries)
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url, heartbeat=30, timeout=30, max_msg_size=0) as ws:
                        self.ws = ws
                        sub_k = json.dumps({
                            'id': 'kline-sub',
                            'reqType': 'sub',
                            'dataType': f'{self.symbol}@kline_{self.interval}'
                        })
                        sub_d = json.dumps({
                            'id': 'depth-sub',
                            'reqType': 'sub',
                            'dataType': f'{self.symbol}@depth10'
                        })
                        await ws.send_str(sub_k)
                        await ws.send_str(sub_d)
                        logger.info('Подписка на %s и %s', f'{self.symbol}@kline_{self.interval}', f'{self.symbol}@depth10')
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._on_message(msg.data)
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                logger.error('WebSocket error: %s', msg.data)
                                break
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error('Ошибка подключения (%s/%s): %s', attempt + 1, max_retries, e)
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay * (2 ** attempt))
                    continue
                else:
                    logger.error('Достигнут лимит переподключений')
                    raise
            except Exception as e:
                logger.exception('Неожиданная ошибка WS: %s', e)
                raise

    async def _on_message(self, msg: str):
        try:
            data = json.loads(msg)
            topic = data.get('dataType')
            if topic is None:
                return
            if topic.startswith(f'{self.symbol}@kline'):
                await self._handle_kline(data)
            elif topic.startswith(f'{self.symbol}@depth'):
                await self._handle_orderbook(data)
        except Exception as e:
            logger.debug('WS parse error: %s', e)

    async def _handle_kline(self, data: Dict[str, Any]):
        c = data.get('data', {})
        try:
            ts = int(c.get('T'))
            new_candle = {
                'timestamp': ts,
                'open': float(c.get('o')),
                'high': float(c.get('h')),
                'low': float(c.get('l')),
                'close': float(c.get('c')),
                'volume': float(c.get('v')) if c.get('v') is not None else 0.0
            }
            if self.candles and self.candles[-1]['timestamp'] == ts:
                self.candles[-1] = new_candle
            else:
                self.candles.append(new_candle)
                if len(self.candles) > MAX_HISTORY:
                    self.candles = self.candles[-MAX_HISTORY:]
            self._last_kline_ts = ts
        except Exception:
            return

    async def _handle_orderbook(self, data: Dict[str, Any]):
        try:
            d = data.get('data', {})
            self.orderbook_top['bids'] = [[float(p), float(s)] for p, s in d.get('bids', [])][:10]
            self.orderbook_top['asks'] = [[float(p), float(s)] for p, s in d.get('asks', [])][:10]
        except Exception:
            return

    def get_candles(self) -> List[Dict[str, Any]]:
        return list(self.candles)

    def get_one_hour_candles(self) -> List[Dict[str, Any]]:
        return list(self.one_hour_candles)

    def get_four_hour_candles(self) -> List[Dict[str, Any]]:
        return list(self.four_hour_candles)

    def get_daily_candles(self) -> List[Dict[str, Any]]:
        return list(self.daily_candles)

    def get_weekly_candles(self) -> List[Dict[str, Any]]:
        return list(self.weekly_candles)

    def get_orderbook(self) -> Dict[str, Any]:
        return dict(self.orderbook_top)

    def get_last_price(self) -> Optional[float]:
        try:
            bids = self.orderbook_top.get('bids') or []
            asks = self.orderbook_top.get('asks') or []
            if bids and asks:
                best_bid = float(bids[0][0])
                best_ask = float(asks[0][0])
                return (best_bid + best_ask) / 2.0
            if self.candles:
                return float(self.candles[-1]['close'])
        except Exception:
            return None
        return None

# ------------------ Indicators / Pattern recognition ------------------
class Indicators:
    @staticmethod
    def calculate_true_range(prev: Dict[str, float], curr: Dict[str, float]) -> float:
        high = curr['high']
        low = curr['low']
        prev_close = prev['close']
        return max(high - low, abs(high - prev_close), abs(low - prev_close))

    @staticmethod
    def calculate_atr(candles: List[Dict[str, float]], period: int = ATR_PERIOD) -> Optional[float]:
        if len(candles) < period + 1:
            return None
        trs: List[float] = []
        for i in range(1, period + 1):
            trs.append(Indicators.calculate_true_range(candles[-i - 1], candles[-i]))
        return sum(trs) / float(period) if trs else None

    @staticmethod
    def find_swings(candles: List[Dict[str, Any]], lookback: int = 2) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        swing_highs: List[Dict[str, Any]] = []
        swing_lows: List[Dict[str, Any]] = []
        if len(candles) < lookback * 2 + 1:
            return swing_highs, swing_lows
        for i in range(lookback, len(candles) - lookback):
            window = candles[i - lookback:i + lookback + 1]
            mid = candles[i]
            if all(mid['high'] >= w['high'] for w in window if w is not mid):
                swing_highs.append({'idx': i, 'timestamp': mid['timestamp'], 'price': mid['high']})
            if all(mid['low'] <= w['low'] for w in window if w is not mid):
                swing_lows.append({'idx': i, 'timestamp': mid['timestamp'], 'price': mid['low']})
        return swing_highs, swing_lows

    @staticmethod
    def detect_structure_trend(swing_highs: List[Dict[str, Any]], swing_lows: List[Dict[str, Any]]) -> int:
        """Возвращает 1 (бычий), -1 (медвежий), 0 (неопределён)."""
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return 0
        hh = swing_highs[-1]['price'] > swing_highs[-2]['price']
        hl = swing_lows[-1]['price'] > swing_lows[-2]['price']
        lh = swing_highs[-1]['price'] < swing_highs[-2]['price']
        ll = swing_lows[-1]['price'] < swing_lows[-2]['price']
        if hh and hl:
            return 1
        if lh and ll:
            return -1
        return 0

    @staticmethod
    def detect_bos_choch(candles: List[Dict[str, Any]], struct_trend: int) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Возвращает (BOS, CHoCH) если обнаружено."""
        if len(candles) < 5:
            return None, None
        swing_highs, swing_lows = Indicators.find_swings(candles)
        if not swing_highs or not swing_lows:
            return None, None
        last_close = candles[-1]['close']
        last_high = swing_highs[-1]['price']
        last_low = swing_lows[-1]['price']
        bos = None
        choch = None
        if struct_trend == 1 and last_close > last_high:
            bos = {'side': 'Buy', 'level': last_high, 'reason': 'BOS_BULLISH'}
        elif struct_trend == -1 and last_close < last_low:
            bos = {'side': 'Sell', 'level': last_low, 'reason': 'BOS_BEARISH'}
        # CHoCH как пробой противоположного экстремума
        if struct_trend == 1 and last_close < last_low:
            choch = {'side': 'Sell', 'level': last_low, 'reason': 'CHoCH_BEARISH'}
        elif struct_trend == -1 and last_close > last_high:
            choch = {'side': 'Buy', 'level': last_high, 'reason': 'CHoCH_BULLISH'}
        return bos, choch

    @staticmethod
    def detect_fvg(candles: List[Dict[str, Any]], lookback: int = 50) -> Optional[Dict[str, Any]]:
        """Ищет последний FVG (Fair Value Gap) по 3-свечной схеме.
        Возвращает словарь с полями: type, lower, upper, mid
        """
        if len(candles) < 3:
            return None
        start = max(2, len(candles) - lookback)
        for i in range(len(candles) - 1, start - 1, -1):
            if i - 2 < 0:
                break
            c1 = candles[i - 2]
            c2 = candles[i - 1]
            c3 = candles[i]
            # Bullish FVG: low of c3 > high of c1
            if c3['low'] > c1['high']:
                lower = c1['high']
                upper = c3['low']
                return {'type': 'FVG_LONG', 'lower': lower, 'upper': upper, 'mid': (lower + upper) / 2.0}
            # Bearish FVG: high of c3 < low of c1
            if c3['high'] < c1['low']:
                lower = c3['high']
                upper = c1['low']
                return {'type': 'FVG_SHORT', 'lower': lower, 'upper': upper, 'mid': (lower + upper) / 2.0}
        return None

    @staticmethod
    def calculate_risk_reward(entry: float, stop: float, take: float) -> float:
        risk = abs(entry - stop)
        reward = abs(take - entry)
        if risk <= 0:
            return 0.0
        return reward / risk

    @staticmethod
    def atr(candles: List[Dict[str, float]], period: int) -> Optional[float]:
        return Indicators.calculate_atr(candles, period)

    @staticmethod
    def is_pivot_high(candles: List[Dict[str, float]], idx: int, lookback: int) -> bool:
        if idx - lookback < 0 or idx + lookback >= len(candles):
            return False
        pivot_high = candles[idx]['high']
        for j in range(idx - lookback, idx + lookback + 1):
            if j == idx:
                continue
            if candles[j]['high'] > pivot_high:
                return False
        return True

    @staticmethod
    def is_pivot_low(candles: List[Dict[str, float]], idx: int, lookback: int) -> bool:
        if idx - lookback < 0 or idx + lookback >= len(candles):
            return False
        pivot_low = candles[idx]['low']
        for j in range(idx - lookback, idx + lookback + 1):
            if j == idx:
                continue
            if candles[j]['low'] < pivot_low:
                return False
        return True

# ------------------ Strategy ------------------
class Strategy:
    def __init__(self, market: MarketData, notifier: Optional[TelegramNotifier] = None):
        self.market = market
        self.notifier = notifier
        self.position: Optional[str] = None  # 'LONG' / 'SHORT' / None
        self.trend_bias = 0  # 1 / -1 / 0
        self._last_eval_ts: Optional[int] = None
        self._cooldown_until_ts: float = 0.0
        # SMC state
        self.swing_bias = 0
        self.internal_bias = 0
        self.swing_high_level: Optional[float] = None
        self.swing_low_level: Optional[float] = None
        self.internal_high_level: Optional[float] = None
        self.internal_low_level: Optional[float] = None
        self.swing_high_crossed = False
        self.swing_low_crossed = False
        self.internal_high_crossed = False
        self.internal_low_crossed = False
        self._last_fvg_type: Optional[str] = None
        self._last_fvg_ts: Optional[int] = None
        self._last_equal_high_ts: Optional[int] = None
        self._last_equal_low_ts: Optional[int] = None

    async def _update_timeframes(self) -> None:
        await asyncio.gather(
            self.market.fetch_one_hour_candles(),
            self.market.fetch_four_hour_candles(),
            self.market.fetch_daily_candles(),
            self.market.fetch_weekly_candles()
        )

    @staticmethod
    def _sequence_from_swings(candles: List[Dict[str, Any]]) -> Optional[str]:
        highs, lows = Indicators.find_swings(candles)
        trend = Indicators.detect_structure_trend(highs, lows)
        if trend == 1:
            return "BULLISH"
        if trend == -1:
            return "BEARISH"
        return None

    def _compute_trend_bias(self, one_h: List[Dict[str, Any]], four_h: List[Dict[str, Any]], daily: List[Dict[str, Any]], weekly: List[Dict[str, Any]]) -> int:
        seqs = [
            self._sequence_from_swings(one_h),
            self._sequence_from_swings(four_h),
            self._sequence_from_swings(daily),
            self._sequence_from_swings(weekly)
        ]
        bull = sum(1 for s in seqs if s == 'BULLISH')
        bear = sum(1 for s in seqs if s == 'BEARISH')
        if bull >= 3:
            return 1
        if bear >= 3:
            return -1
        return 0

    async def evaluate(self) -> Optional[Dict[str, Any]]:
        logger.debug('Strategy.evaluate tick')
        candles = self.market.get_candles()
        if len(candles) < MIN_HISTORY_FOR_SIGNAL:
            logger.debug('Skip: not enough candles (%s < %s)', len(candles), MIN_HISTORY_FOR_SIGNAL)
            return None
        # Evaluate только на новой свече
        last_ts = candles[-1]['timestamp']
        if self._last_eval_ts == last_ts:
            logger.debug('Skip: no new candle (last_ts=%s)', last_ts)
            return None
        self._last_eval_ts = last_ts

        await self._update_timeframes()
        one_h = self.market.get_one_hour_candles()
        four_h = self.market.get_four_hour_candles()
        daily = self.market.get_daily_candles()
        weekly = self.market.get_weekly_candles()

        self.trend_bias = self._compute_trend_bias(one_h, four_h, daily, weekly)
        logger.debug('Trend bias=%s', self.trend_bias)

        atr = Indicators.calculate_atr(candles, ATR_PERIOD)
        if atr is None:
            logger.debug('Skip: ATR is None')
            return None

        last_price = self.market.get_last_price()
        if last_price is None:
            logger.debug('Skip: last_price is None')
            return None

        # Фильтр по спреду
        ob = self.market.get_orderbook()
        bids = ob.get('bids') or []
        asks = ob.get('asks') or []
        if bids and asks:
            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
            spread = best_ask - best_bid
            if spread / last_price > 0.0004:  # 4 б.п.
                logger.debug('Skip: spread too high (spread=%s)', spread)
                return None

        # Фильтр волатильности: ATR/Price должен быть в адекватном диапазоне
        atr_ratio = atr / last_price
        if not (0.0005 <= atr_ratio <= 0.03):  # 5 б.п. - 3%
            logger.debug('Skip: atr_ratio out of range (%.6f)', atr_ratio)
            return None

        # ---------- SMC-like swing/internal structure and alerts ----------
        await self._smc_alerts(candles)

        swing_highs, swing_lows = Indicators.find_swings(candles, lookback=2)
        struct_trend = Indicators.detect_structure_trend(swing_highs, swing_lows)
        bos, choch = Indicators.detect_bos_choch(candles, struct_trend)
        fvg = Indicators.detect_fvg(candles, lookback=80)
        logger.debug('Signals: bos=%s, choch=%s, fvg=%s', bool(bos), bool(choch), bool(fvg))

        # Кулдаун
        if time.time() < self._cooldown_until_ts:
            logger.debug('Skip: cooldown (%.1fs left)', self._cooldown_until_ts - time.time())
            return None

        candidate: Optional[Dict[str, Any]] = None

        def make_levels(side: str) -> Tuple[float, float, float]:
            # Стоп за последним swing с буфером ATR
            if side == 'Buy' and swing_lows:
                stop = min(s['price'] for s in swing_lows[-3:]) - 0.2 * atr
            elif side == 'Sell' and swing_highs:
                stop = max(s['price'] for s in swing_highs[-3:]) + 0.2 * atr
            else:
                # запасной вариант
                stop = last_price - 1.5 * atr if side == 'Buy' else last_price + 1.5 * atr
            entry = last_price
            risk = abs(entry - stop)
            take = entry + 2.5 * risk if side == 'Buy' else entry - 2.5 * risk
            return entry, stop, take

        # Логика сигналов с учётом MTF bias
        if bos and ((self.trend_bias == 1 and bos['side'] == 'Buy') or (self.trend_bias == -1 and bos['side'] == 'Sell')):
            entry, stop, take = make_levels(bos['side'])
            rr = Indicators.calculate_risk_reward(entry, stop, take)
            logger.debug('BOS candidate rr=%.2f', rr)
            if rr >= MIN_RR:
                candidate = {'side': bos['side'], 'level': entry, 'stop': stop, 'take': take, 'reason': bos['reason']}
        elif choch and self.trend_bias == 0:
            entry, stop, take = make_levels(choch['side'])
            rr = Indicators.calculate_risk_reward(entry, stop, take)
            logger.debug('CHoCH candidate rr=%.2f', rr)
            if rr >= MIN_RR:
                candidate = {'side': choch['side'], 'level': entry, 'stop': stop, 'take': take, 'reason': choch['reason']}

        # Дополнительный фильтр по FVG: цена должна быть внутри зоны FVG +/- 0.25 ATR
        if candidate and fvg:
            side = candidate['side']
            within = False
            lower = upper = None
            if side == 'Buy' and fvg['type'] == 'FVG_LONG':
                lower, upper = fvg['lower'], fvg['upper']
                within = (lower - 0.25 * atr) <= last_price <= (upper + 0.25 * atr)
            elif side == 'Sell' and fvg['type'] == 'FVG_SHORT':
                lower, upper = fvg['lower'], fvg['upper']
                within = (lower - 0.25 * atr) <= last_price <= (upper + 0.25 * atr)
            logger.debug('FVG filter within=%s zone=[%s,%s] price=%.4f', within, lower, upper, last_price)
            if not within:
                candidate = None

        if candidate:
            # Устанавливаем кулдаун
            self._cooldown_until_ts = time.time() + COOLDOWN_SECS
            # Telegram notify
            if self.notifier:
                txt = (
                    f"<b>Signal</b> {candidate['side']} {SYMBOL} ({INTERVAL})\n"
                    f"Price: <code>{candidate['level']:.4f}</code>\n"
                    f"Stop: <code>{candidate['stop']:.4f}</code>  Take: <code>{candidate['take']:.4f}</code>\n"
                    f"Reason: <i>{candidate['reason']}</i>"
                )
                asyncio.create_task(self.notifier.send(txt))
            logger.info('Signal: %s %s at %s (reason=%s, Stop: %s, Take: %s)', candidate['side'], SYMBOL, round(candidate['level'], 2), candidate['reason'], round(candidate['stop'], 2), round(candidate['take'], 2))
            return candidate

        logger.debug('No candidate this tick')
        return None

    # ---------- SMC helpers ----------
    async def _smc_alerts(self, candles: List[Dict[str, Any]]) -> None:
        """Approximate Pine SMC alerts: swing/internal BOS/CHoCH, FVG, EQH/EQL."""
        if len(candles) < max(SWINGS_LENGTH * 2 + 5, 10):
            return
        last_close = candles[-1]['close']
        last_ts = candles[-1]['timestamp']
        atr200 = Indicators.atr(candles, 200) or 0.0

        # Compute last confirmed swing pivots (using symmetrical lookback)
        # Swing pivot index is len-1-SWINGS_LENGTH to ensure we have right-side bars for confirmation
        swing_idx = len(candles) - 1 - SWINGS_LENGTH
        if swing_idx > 0 and swing_idx + SWINGS_LENGTH < len(candles):
            if Indicators.is_pivot_high(candles, swing_idx, SWINGS_LENGTH):
                level = candles[swing_idx]['high']
                if level != self.swing_high_level:
                    self.swing_high_level = level
                    self.swing_high_crossed = False
            if Indicators.is_pivot_low(candles, swing_idx, SWINGS_LENGTH):
                level = candles[swing_idx]['low']
                if level != self.swing_low_level:
                    self.swing_low_level = level
                    self.swing_low_crossed = False

        # Internal pivots with smaller lookback
        internal_idx = len(candles) - 1 - INTERNAL_LENGTH
        if internal_idx > 0 and internal_idx + INTERNAL_LENGTH < len(candles):
            if Indicators.is_pivot_high(candles, internal_idx, INTERNAL_LENGTH):
                level = candles[internal_idx]['high']
                if level != self.internal_high_level:
                    self.internal_high_level = level
                    self.internal_high_crossed = False
            if Indicators.is_pivot_low(candles, internal_idx, INTERNAL_LENGTH):
                level = candles[internal_idx]['low']
                if level != self.internal_low_level:
                    self.internal_low_level = level
                    self.internal_low_crossed = False

        # Cross logic -> BOS/CHoCH
        # Swing high cross
        if self.swing_high_level is not None and not self.swing_high_crossed and last_close > self.swing_high_level:
            tag = 'CHoCH' if self.swing_bias == -1 else 'BOS'
            self.swing_high_crossed = True
            self.swing_bias = 1
            if self.notifier:
                asyncio.create_task(self.notifier.send(f"<b>Swing Bullish {tag}</b> on {SYMBOL} ({INTERVAL})\nLevel: <code>{self.swing_high_level:.4f}</code>"))
        # Swing low cross
        if self.swing_low_level is not None and not self.swing_low_crossed and last_close < self.swing_low_level:
            tag = 'CHoCH' if self.swing_bias == 1 else 'BOS'
            self.swing_low_crossed = True
            self.swing_bias = -1
            if self.notifier:
                asyncio.create_task(self.notifier.send(f"<b>Swing Bearish {tag}</b> on {SYMBOL} ({INTERVAL})\nLevel: <code>{self.swing_low_level:.4f}</code>"))

        # Internal high cross
        if self.internal_high_level is not None and not self.internal_high_crossed and last_close > self.internal_high_level:
            tag = 'CHoCH' if self.internal_bias == -1 else 'BOS'
            self.internal_high_crossed = True
            self.internal_bias = 1
            if self.notifier:
                asyncio.create_task(self.notifier.send(f"<b>Internal Bullish {tag}</b> on {SYMBOL} ({INTERVAL})\nLevel: <code>{self.internal_high_level:.4f}</code>"))
        # Internal low cross
        if self.internal_low_level is not None and not self.internal_low_crossed and last_close < self.internal_low_level:
            tag = 'CHoCH' if self.internal_bias == 1 else 'BOS'
            self.internal_low_crossed = True
            self.internal_bias = -1
            if self.notifier:
                asyncio.create_task(self.notifier.send(f"<b>Internal Bearish {tag}</b> on {SYMBOL} ({INTERVAL})\nLevel: <code>{self.internal_low_level:.4f}</code>"))

        # FVG on current TF
        if len(candles) >= 3:
            c1 = candles[-3]
            c3 = candles[-1]
            if c3['low'] > c1['high']:
                if self._last_fvg_ts != last_ts or self._last_fvg_type != 'FVG_LONG':
                    self._last_fvg_ts = last_ts
                    self._last_fvg_type = 'FVG_LONG'
                    lower, upper = c1['high'], c3['low']
                    if self.notifier:
                        asyncio.create_task(self.notifier.send(f"<b>Bullish FVG</b> on {SYMBOL} ({INTERVAL})\nZone: <code>{lower:.4f} - {upper:.4f}</code>"))
            elif c3['high'] < c1['low']:
                if self._last_fvg_ts != last_ts or self._last_fvg_type != 'FVG_SHORT':
                    self._last_fvg_ts = last_ts
                    self._last_fvg_type = 'FVG_SHORT'
                    lower, upper = c3['high'], c1['low']
                    if self.notifier:
                        asyncio.create_task(self.notifier.send(f"<b>Bearish FVG</b> on {SYMBOL} ({INTERVAL})\nZone: <code>{lower:.4f} - {upper:.4f}</code>"))

        # Equal Highs / Lows
        # Use last two swing highs/lows from small lookback to approximate
        hs, ls = Indicators.find_swings(candles, lookback=EQUAL_LENGTH)
        if atr200 > 0 and len(hs) >= 2:
            d = abs(hs[-1]['price'] - hs[-2]['price'])
            if d <= EQUAL_THRESHOLD * atr200 and (self._last_equal_high_ts != hs[-1]['timestamp']):
                self._last_equal_high_ts = hs[-1]['timestamp']
                if self.notifier:
                    asyncio.create_task(self.notifier.send(f"<b>Equal Highs</b> on {SYMBOL} ({INTERVAL})\nLevel: <code>{hs[-1]['price']:.4f}</code>"))
        if atr200 > 0 and len(ls) >= 2:
            d = abs(ls[-1]['price'] - ls[-2]['price'])
            if d <= EQUAL_THRESHOLD * atr200 and (self._last_equal_low_ts != ls[-1]['timestamp']):
                self._last_equal_low_ts = ls[-1]['timestamp']
                if self.notifier:
                    asyncio.create_task(self.notifier.send(f"<b>Equal Lows</b> on {SYMBOL} ({INTERVAL})\nLevel: <code>{ls[-1]['price']:.4f}</code>"))

    def on_position_update(self, pos_side: Optional[str]):
        self.position = pos_side

# ------------------ Main loop ------------------
async def main_loop():
    logger.info("Запуск основного цикла")
    market = MarketData(SYMBOL, INTERVAL)
    notifier = TelegramNotifier(TG_BOT_TOKEN, TG_CHAT_ID)
    strategy = Strategy(market, notifier=notifier)

    # Инициализация историей до старта WS
    await market.seed_current_interval(limit=300)

    async def ws_runner():
        while True:
            try:
                await market.connect_ws()
            except Exception as e:
                logger.exception('WS error, reconnecting in 5s: %s', e)
                await asyncio.sleep(5)

    ws_task = asyncio.create_task(ws_runner())
    logger.debug('WS runner started')

    try:
        while True:
            try:
                logger.debug('Main loop tick')
                signal = await strategy.evaluate()
                if signal:
                    logger.info('Signal: %s %s at %s (reason=%s, Stop: %s, Take: %s)', signal['side'], SYMBOL, round(signal['level'], 2), signal['reason'], round(signal['stop'], 2), round(signal['take'], 2))
            except Exception as e:
                logger.error('Ошибка стратегии: %s', e)
            await asyncio.sleep(2)
    except asyncio.CancelledError:
        logger.info('Main loop cancelled')
    finally:
        ws_task.cancel()

if __name__ == '__main__':
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info('Остановлено пользователем')
    except Exception as e:
        logger.error("Ошибка в основном цикле: %s", e)