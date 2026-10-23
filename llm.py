import json
import re
import os
from datetime import datetime
import pandas as pd
import requests
import logging

# Логгер для модуля llm
logger = logging.getLogger(__name__)




class GoRouterAnalyst:
    """
    Аналитик на базе GoRouter API (модель gorouter/free).
    Интерфейс совместим с прежним LocalHFModel:
      - parse_user_query(query, available_shifts)
      - generate_response(query, data_result, metrics_type)
    """

    API_URL = "https://gorouter.ru/api/v1/chat/completions"
    # MODEL = "stealth/space-bunny-alpha"
    # MODEL = "gorouter/free"
    #MODEL = "inclusionai/ling-3.0-flash-sante:free"
    """"
    FALLBACK_MODELS = [
        "inclusionai/ling-3.0-flash-sante:free",
        "gorouter/free",
        "stealth/space-bunny-alpha", 
        "inclusionai/ling-3.1-flash" 
        deepseek/deepseek-v4-flash-0731
    ]

    """
    FALLBACK_MODELS = [
        "gorouter/free"
    ]

    def __init__(self, api_key=None,model=None):
        self.api_key = api_key or os.environ.get("GOROUTER_API_KEY")
        if not self.api_key:
            raise ValueError(
                "Не задан GOROUTER_API_KEY. Установите переменную окружения "
                "или передайте ключ в конструктор."
            )
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # Простая проверка соединения
        if model:
            self.FALLBACK_MODELS = [model]
        logger.info(f"GoRouter готов к работе. Модели: {self.FALLBACK_MODELS}")
        

    def _call_llm(self, messages, temperature=0.0, max_tokens=4000):
        """
        Универсальный вызов Chat Completions.
        messages: список dict-ов [{'role': 'system'|'user'|'assistant', 'content': '...'}]
        Возвращает строку с ответом модели.
        """
        last_error=None
        for model in self.FALLBACK_MODELS:
            logger.info(f"→ Пробуем модель: {model}")
            try:
                payload = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                response = requests.post(
                    self.API_URL,
                    headers=self.headers,
                    json=payload,
                    timeout=120,
                )
                response.raise_for_status()
                data = response.json()
            # Безопасно достаём сообщение
                choices = data.get("choices") or []
                if not choices:
                    last_error = f"Пустой ответ от {model}"
                    logger.warning(f"  ✗ {last_error}")
                    continue
                message = choices[0].get("message") or {}    
 # Пробуем разные поля, куда модель может положить текст
                content = message.get("content")
                if content is None:
                # Некоторые модели кладут текст в reasoning_content
                    content = message.get("reasoning_content")
            # Если content — список (бывает у некоторых API), соберём текст
                if isinstance(content, list):
                    parts = []
                    for item in content:
                        if isinstance(item, dict) and "text" in item:
                            parts.append(item["text"])
                        elif isinstance(item, str):
                            parts.append(item)
                    content = " ".join(parts)

             # Если ответ всё ещё пустой — пробуем следующую модель
                if content is None or not str(content).strip():
                    last_error = f"Модель {model}: пустой content"
                    logger.warning(f"  ✗ {last_error}")
                    continue

                logger.info(f"  ✓ Успешно ответила модель: {model}")
                return str(content).strip()
            
            except requests.exceptions.HTTPError as e:
                try:
                    body = e.response.text[:300]
                except Exception:
                    body = ""
                last_error = f"Модель {model}: HTTP {e} | {body}"
                logger.error(f"  ✗ {last_error}")
                continue  # ← пробуем следующую модель

            except Exception as e:
                last_error = f"Модель {model}: {e}"
                logger.error(f"  ✗ {last_error}")
                continue  # ← пробуем следующую модель
            # Если ни одна модель не сработала
        logger.error(f"  ✗✗ Все модели не сработали. Последняя ошибка: {last_error}")
        return f"ОШИБКА: все модели недоступны. Последняя ошибка: {last_error}"
        



      # ------------------------------------------------------------------
    # ПАРСИНГ ЗАПРОСА ПОЛЬЗОВАТЕЛЯ
    def parse_user_query(self, user_query, available_shifts=None):
        today = datetime.now().date()
        system_prompt = (
            "Ты — парсер запросов производственного аналитика. "
            "Твоя задача — извлечь из запроса пользователя параметры и вернуть ТОЛЬКО "
            "валидный JSON-объект без пояснений и без markdown-разметки."
        )

        user_prompt = f"""Дата сегодня: {today}.
Доступные смены (пример): 2025-12-27_День, 2025-12-27_Ночь

Поля результирующего JSON:
- shift_list: список строк формата "ГГГГ-ММ-ДД_День" или "ГГГГ-ММ-ДД_Ночь"
- start_date: строка "ГГГГ-ММ-ДД" (если указан диапазон дат)
- end_date: строка "ГГГГ-ММ-ДД"
- start_shift_type: "День" или "Ночь"
- end_shift_type: "День" или "Ночь"
- machines: список строк (номера станков, если указаны)
- operators: список строк (ФИО, если указаны)
- metrics_type: "kim" | "downtime" | "operator" | "hourly"
- aggregation: "detailed" | "period"

Правила:
- Если пользователь не указал даты — НЕ включай start_date/end_date.
- Если упоминается оператор — ставь metrics_type="operator" и заполняй operators.
- Если спрашивают про простои — metrics_type="downtime".
- Если спрашивают про загрузку по часам — metrics_type="hourly".
- Иначе metrics_type="kim".

Дополнение:
- Пользователь может указать станок толькко через номер вначале, например станок 3.4_АРМА-S600 пользователь может назвать 3.4
Примеры:

Запрос: "Комов Александр Викторович, Краснов Юрий Андреевич"
Ответ: {{"metrics_type": "operator", "aggregation": "period", "operators": ["Комов Александр Викторович, Краснов Юрий Андреевич]"}}

Запрос: "Работа оператора Комов Александр Викторович"
Ответ: {{"metrics_type": "operator", "aggregation": "period", "operators": ["Комов Александр Викторович]" }}

Запрос: "В какие смены работал оператор Краснов Юрий Андреевич"
Ответ: {{"metrics_type": "operator", "aggregation": "period", "operators": ["Краснов Юрий Андреевич]"}}

Запрос: "КИМ станка 3.6_АРМА-S600 за 27 декабря 2025"
Ответ: {{"start_date": "2025-12-27", "end_date": "2025-12-27", "machines": ["3.6_АРМА-S600"], "metrics_type": "kim", "aggregation": "period"}}

Запрос: "Производительность станка 3.4 за 27 декабря 2025"
Ответ: {{"start_date": "2025-12-27", "end_date": "2025-12-27", "machines": ["3.4_АРМА-S600"], "metrics_type": "kim", "aggregation": "period"}}

Запрос: "Простои в ночную смену 27 декабря"
Ответ: {{"shift_list": ["2025-12-27_Ночь"], "metrics_type": "downtime", "aggregation": "detailed"}}

Запрос: "Сводка по операторам за 28 декабря"
Ответ: {{"start_date": "2025-12-28", "end_date": "2025-12-28", "metrics_type": "operator", "aggregation": "period"}}

Запрос: "Топ-5 операторов по производительности за 27 декабря"
Ответ: {{"start_date": "2025-12-27", "end_date": "2025-12-27", "metrics_type": "operator", "aggregation": "period"}}



Запрос: {user_query}
Ответ:"""
        response_text = self._call_llm(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=4000,
        )

        # Обработка ошибок API
        if response_text.startswith("ОШИБКА"):
            logger.error(f"LLM вернула ошибку при парсинге: {response_text}")
            return {"error": response_text}

        # Извлекаем первый сбалансированный JSON-объект
        start = response_text.find("{")
        if start == -1:
            logger.error(f"JSON не найден в ответе: {response_text[:200]}")
            return {"error": "JSON не найден", "raw_response": response_text[:300]}

        brace_count = 0
        end = -1
        for i, ch in enumerate(response_text[start:], start):
            if ch == "{":
                brace_count += 1
            elif ch == "}":
                brace_count -= 1
                if brace_count == 0:
                    end = i + 1
                    break
        if end == -1:
            logger.error(f"Незакрытый JSON: {response_text[:200]}")
            return {"error": "Незакрытый JSON", "raw_response": response_text[:300]}

        json_str = response_text[start:end]
        try:
            params = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка разбора JSON: {e} | {json_str}")
            return {"error": f"Ошибка JSON: {e}", "raw_response": json_str}

        # Чистка некорректного shift_list
        if "shift_list" in params:
            sl = params["shift_list"]
            if not isinstance(sl, list) or not all(
                isinstance(x, str) and "_" in x for x in sl
            ):
                del params["shift_list"]

        # Преобразование дат
        for key in ("start_date", "end_date"):
            if key in params and params[key]:
                try:
                    params[key] = datetime.strptime(params[key], "%Y-%m-%d").date()
                except Exception:
                    params.pop(key, None)

        logger.info(f"Распарсенные параметры: {params}")
        return params

  
    def generate_response(self, user_query, data_result, metrics_type):
        # Преобразуем данные в компактный текстовый вид
        if isinstance(data_result, pd.DataFrame):
            if data_result.empty:
                data_text = "Данные отсутствуют."
            else:
                # Ограничим до 30 строк, чтобы не раздувать контекст
                data_text = data_result.head(30).reset_index().to_string(index=False)
        elif isinstance(data_result, pd.Series):
            data_text = data_result.to_string()
        else:
            data_text = str(data_result)




        
        system_prompt = f"""Ты производственный аналитик. На основе предоставленных данных ответь на запрос пользователя. 
        Ответ должен быть кратким, содержать только факты и не содержать вопросов.
        Не выводи в ответ таблицу строки таблиц, которые получаешь.
        Если работаешь с метрикой "kim", укажи процент полезного использования оборудования (% полезной работы). 
        Если работаешь с метрикой "operator", то нужно рассказать, какой процент незарегистрированного простоя оборудования (%_неотмеченного_простоя) у оператора,
        сколько минут он был зарегистрирован (Всего_минут) и какое продуктивное время в минутах (Продуктивное_время_мин) и в процентах (%_продуктивного времени), и нужно вывести информацию, на каких станках работал оператор (Обслуживал_станки).
        Если пользователь спрашивает, в какие смены работал оператор, то выведи список смен (работал_в_смены), например 2025-12-28_Ночь, 2025-12-29_Ночь (если не просит, не выводи). 
        Если пользователь просит вывести топ-n операторов производительности, то нужно вывести два списка в порядке убывания значения: 1) список n операторов с наивысшим показателем "%_продуктивного времени", 2) список n операторов с наивысшим показателем Продуктивное_время_мин.  
        Если работаешь с метрикой "downtime" - то в столбце "вид простоя" указан вид простоя, а в стлбце "Доля_%" указан процент этого простоя, первая строка данных - общая сумма простоя оборудования; дай топ-5 простоев оборуодвания в формате ["вид простоя" - "минуты" или "Доля_%"], соотнеси вид простоя из первого столбца с данными о простоях из столбцов 2 и 3;
        Сумма состояний "станок включен" и "jog" составляет значение неотмеченного простоя оборудования.
        """
        

        user_prompt= f"""Запрос пользователя: {user_query}
Тип метрики: {metrics_type}
Данные (первые строки):
{data_text}
Ответ:"""
        answer = self._call_llm(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=4000,
        )
        logger.info(f"Ответ аналитика сформирован ({len(answer)} симв.)")
        return answer