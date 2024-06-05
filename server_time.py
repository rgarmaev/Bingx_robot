import requests

APIURL = "https://open-api.bingx.com"

def get_server_time():
    url = f"{APIURL}/openApi/swap/v2/quote/time"
    response = requests.get(url)
    if response.status_code == 200:
        try:
            response_json = response.json()
            print("Ответ JSON:", response_json)  # выводим полный JSON-ответ для отладки
            server_time = response_json['serverTime']
            return server_time
        except KeyError:
            print("Ошибка: ключ 'serverTime' не найден в JSON-ответе.")
            return None
    else:
        print(f"Ошибка: не удалось получить серверное время. Статус код: {response.status_code}")
        return None

def parseParam(paramsMap):
    paramsStr = ""
    for key, value in paramsMap.items():
        paramsStr += f"{key}={value}&"
    timestamp = get_server_time()
    if timestamp is None:
        raise ValueError("Не удалось получить серверное время")
    return paramsStr + "timestamp=" + str(int(timestamp))

def demo():
    server_time = get_server_time()
    if server_time is not None:
        print(f"Серверное время: {server_time}")
    else:
        print("Не удалось получить серверное время.")
    paramsMap = {'param1': 'value1', 'param2': 'value2'}
    try:
        paramsStr = parseParam(paramsMap)
        print(f"Параметры: {paramsStr}")
    except ValueError as e:
        print(e)
    return "Demo завершено"

if __name__ == "__main__":
    print("demo:", demo())
