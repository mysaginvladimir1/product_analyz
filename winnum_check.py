"""Offline configuration check; --probe performs only read-only Winnum requests."""
import argparse
import sys
from datetime import date

from dotenv import load_dotenv
from winnum_client import WinnumClient, WinnumConfig, WinnumError
from winnum_plan import Clarification
from winnum_service import WinnumService


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', action='store_true', help='Прочитать справочник оборудования')
    parser.add_argument('--machine', help='Название станка для проверки данных; нужен --date')
    parser.add_argument('--date', type=date.fromisoformat, help='Завершённый день YYYY-MM-DD')
    args = parser.parse_args()
    if bool(args.machine) != bool(args.date) or (args.machine and not args.probe):
        parser.error('--machine и --date указываются вместе с --probe')
    try:
        config = WinnumConfig.from_env()
        print('Конфигурация Winnum заполнена. Учётные данные скрыты.')
        if not args.probe:
            print('Сеть не проверялась. Для проверки в локальной сети добавьте --probe.')
            return 0
        client = WinnumClient(config)
        products = client.products()
        print(f'Справочник получен: {len(products)} станков.')
        if args.machine:
            service = WinnumService(client)
            for intent in ('operator_machines', 'downtime'):
                result = service.execute({'intent': intent, 'start_date': args.date,
                                          'end_date': args.date, 'machines': [args.machine]})
                print(f'{intent}: {result["row_count"]} строк; предупреждений: {len(result["warnings"])}')
                for warning in result['warnings']:
                    print(warning)
        return 0
    except (WinnumError, Clarification) as exc:
        print(str(exc))
        return 1


if __name__ == '__main__':
    sys.exit(main())
