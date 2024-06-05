import time
import requests
import hmac
from hashlib import sha256

APIURL = "https://open-api.bingx.com"
APIKEY = "EuW8fOL9il9ywUiI1qjzb06WkgwbOIGfIZANTcLOq8QHj4jfVsnGykgqZRdBRdfBjUZpigiLoyfRnj3rMSw"
SECRETKEY = "35MrDlIprD0zPupEaTKG1xwCxGlvVMGKAUAu0A2wBuDuUtcoACwLPGBRbXK3H20sT4YoD6srm17XjCTVtUr8g"


def demo():
    payload = {}
    path = '/openApi/swap/v1/ticker/price'
    method = "GET"
    paramsMap = {
        "symbol": "BTC-USDT"
    }
    paramsStr = parseParam(paramsMap)
    response = send_request(method, path, paramsStr, payload)

    # Проверяем наличие поля 'data' и 'price' в ответе
    if 'data' in response and 'price' in response['data']:
        latest_price = response['data']['price']
        current_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
        return f'{current_time}, {paramsMap["symbol"]}, {latest_price}'
    else:
        # Выводим полный ответ для отладки
        print("Полный ответ:", response)
        return "Не удалось получить последнюю цену."


def get_sign(api_secret, payload):
    signature = hmac.new(api_secret.encode("utf-8"), payload.encode("utf-8"), digestmod=sha256).hexdigest()
    return signature


def send_request(method, path, urlpa, payload):
    url = f"{APIURL}{path}?{urlpa}&signature={get_sign(SECRETKEY, urlpa)}"
    headers = {
        'X-BX-APIKEY': APIKEY,
    }
    response = requests.request(method, url, headers=headers, data=payload)

    response_json = response.json()
    return response_json


def parseParam(paramsMap):
    sortedKeys = sorted(paramsMap)
    paramsStr = "&".join([f"{x}={paramsMap[x]}" for x in sortedKeys])
    if paramsStr:
        return paramsStr + "&timestamp=" + str(int(time.time() * 1000))
    else:
        return "timestamp=" + str(int(time.time() * 1000))


if __name__ == '__main__':
    print(demo())
