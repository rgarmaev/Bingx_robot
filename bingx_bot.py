import os
import asyncio
import json
import hmac
import hashlib
import time
from typing import Dict, Any, List, Optional, Tuple

import aiohttp
import logging

# Конфигурация логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
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

SYMBOL = os.getenv('SYMBOL', 'BTC-USDT')
INTERVAL = os.getenv('INTERVAL', '15m')
USE_TESTNET = os.getenv('USE_TESTNET', 'false').lower() in ('1', 'true', 'yes')
API_KEY, API_SECRET = read_api_credentials()

# BingX V2 endpoints
WS_REST_PUBLIC = 'wss://open-api-ws.bingx.com/market'
REST_BASE = 'https://open-api.bingx.com'

# Parameters to tune
MAX_HISTORY = 1500  # кол-во свечей текущего ТФ
MIN_HISTORY_FOR_SIGNAL = 120  # минимум свечей на ТФ для сигналов
ATR_PERIOD = 14
MIN_RR = 2.0
COOLDOWN_SECS = 90  # кулдаун между сигналами

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
    def find_swings(candles: List[Dict[str, float]], lookback: int = 2) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
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

# ------------------ Strategy ------------------
class Strategy:
    def __init__(self, market: MarketData):
        self.market = market
        self.position: Optional[str] = None  # 'LONG' / 'SHORT' / None
        self.trend_bias = 0  # 1 / -1 / 0
        self._last_eval_ts: Optional[int] = None
        self._cooldown_until_ts: float = 0.0

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
        candles = self.market.get_candles()
        if len(candles) < MIN_HISTORY_FOR_SIGNAL:
            return None
        # Evaluate только на новой свече
        last_ts = candles[-1]['timestamp']
        if self._last_eval_ts == last_ts:
            return None
        self._last_eval_ts = last_ts

        await self._update_timeframes()
        one_h = self.market.get_one_hour_candles()
        four_h = self.market.get_four_hour_candles()
        daily = self.market.get_daily_candles()
        weekly = self.market.get_weekly_candles()

        self.trend_bias = self._compute_trend_bias(one_h, four_h, daily, weekly)

        atr = Indicators.calculate_atr(candles, ATR_PERIOD)
        if atr is None:
            return None

        last_price = self.market.get_last_price()
        if last_price is None:
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
                return None

        # Фильтр волатильности: ATR/Price должен быть в адекватном диапазоне
        atr_ratio = atr / last_price
        if not (0.0005 <= atr_ratio <= 0.03):  # 5 б.п. - 3%
            return None

        swing_highs, swing_lows = Indicators.find_swings(candles, lookback=2)
        struct_trend = Indicators.detect_structure_trend(swing_highs, swing_lows)
        bos, choch = Indicators.detect_bos_choch(candles, struct_trend)
        fvg = Indicators.detect_fvg(candles, lookback=80)

        # Кулдаун
        if time.time() < self._cooldown_until_ts:
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
            if rr >= MIN_RR:
                candidate = {'side': bos['side'], 'level': entry, 'stop': stop, 'take': take, 'reason': bos['reason']}
        elif choch and self.trend_bias == 0:
            # Берём только когда HTF не определён, торгуем разворот CHoCH
            entry, stop, take = make_levels(choch['side'])
            rr = Indicators.calculate_risk_reward(entry, stop, take)
            if rr >= MIN_RR:
                candidate = {'side': choch['side'], 'level': entry, 'stop': stop, 'take': take, 'reason': choch['reason']}

        # Дополнительный фильтр по FVG: цена должна быть внутри зоны FVG +/- 0.25 ATR
        if candidate and fvg:
            side = candidate['side']
            within = False
            if side == 'Buy' and fvg['type'] == 'FVG_LONG':
                lower, upper = fvg['lower'], fvg['upper']
                within = (lower - 0.25 * atr) <= last_price <= (upper + 0.25 * atr)
            elif side == 'Sell' and fvg['type'] == 'FVG_SHORT':
                lower, upper = fvg['lower'], fvg['upper']
                within = (lower - 0.25 * atr) <= last_price <= (upper + 0.25 * atr)
            if not within:
                candidate = None

        if candidate:
            # Устанавливаем кулдаун
            self._cooldown_until_ts = time.time() + COOLDOWN_SECS
            return candidate

        return None

    def on_position_update(self, pos_side: Optional[str]):
        self.position = pos_side

# ------------------ Executor (REST) ------------------
class Executor:
    def __init__(self, api_key: Optional[str], api_secret: Optional[str]):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base = REST_BASE

    async def place_order(self, symbol: str, side: str, qty: float, price: Optional[float] = None, reduce_only: bool = False) -> Dict[str, Any]:
        """Упрощённый MARKET-ордер BingX V2.
        Примечание: В ряде случаев BingX ожидает query+sign, а не чистый JSON. Здесь оставлена простая форма.
        """
        path = '/openApi/swap/v2/trade/order'
        url = self.base + path
        ts = int(time.time() * 1000)
        params = {
            'symbol': symbol,
            'side': side.upper(),
            'type': 'MARKET',
            'quantity': str(qty),
            'timestamp': str(ts)
        }
        if reduce_only:
            params['reduceOnly'] = 'true'

        if not self.api_secret or not self.api_key:
            return {'code': -1, 'msg': 'missing_api_keys', 'params': params}

        query_str = '&'.join([f"{k}={params[k]}" for k in sorted(params)])
        sign = hmac.new(self.api_secret.encode(), query_str.encode(), hashlib.sha256).hexdigest()
        params['sign'] = sign

        async with aiohttp.ClientSession() as session:
            headers = {
                'Content-Type': 'application/json',
                'X-BX-APIKEY': self.api_key
            }
            async with session.post(url, json=params, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                text = await resp.text()
                try:
                    result = json.loads(text)
                    if result.get('code') != 0:
                        logger.error('Order failed: %s', result.get('msg'))
                    return result
                except Exception as e:
                    logger.error('Failed to parse response: %s, error: %s', text, e)
                    return {'raw': text, 'error': str(e)}

# ------------------ Main loop ------------------
async def main_loop():
    logger.info("Запуск основного цикла")
    market = MarketData(SYMBOL, INTERVAL)
    strategy = Strategy(market)
    executor = Executor(API_KEY, API_SECRET)

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

    try:
        while True:
            try:
                signal = await strategy.evaluate()
                if signal:
                    qty = float(os.getenv('QTY', '0.001'))
                    if executor.api_key and executor.api_secret:
                        logger.info('Постановка ордера: %s %s по %s (Stop: %s, Take: %s, %s)', signal['side'], SYMBOL, round(signal['level'], 2), round(signal['stop'], 2), round(signal['take'], 2), signal['reason'])
                        res = await executor.place_order(SYMBOL, signal['side'], qty)
                        logger.info('Ответ ордера: %s', res)
                    else:
                        logger.info('СИМУЛЯЦИЯ: %s %s по %s (причина=%s, Stop: %s, Take: %s)', signal['side'], SYMBOL, round(signal['level'], 2), signal['reason'], round(signal['stop'], 2), round(signal['take'], 2))
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