# eBay Flip Bot

Telegram-бот, який відстежує вибрані моделі товарів на eBay і сповіщає,
коли ціна лота помітно нижча за поточну "ринкову" (медіану активних
оголошень).

## 1. Що потрібно отримати заздалегідь

1. **Telegram bot token** — напиши @BotFather в Telegram, `/newbot`,
   скопіюй токен.
2. **eBay API ключі**:
   - Зареєструйся на https://developer.ebay.com/
   - Створи "keyset" (Application) у розділі "Application Keys"
   - Візьми `Client ID` та `Client Secret` (production, не sandbox)
   - Ключі безкоштовні, ліміт для базового tier зазвичай достатній для
     персонального використання (перевір актуальні ліміти в кабінеті)

## 2. Встановлення на сервер (Ubuntu, через SSH)

```bash
cd ~
git clone <твій репозиторій або перенеси файли через scp/sftp>
cd ebay_flip_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. Налаштування

Встанови змінні середовища (наприклад, у `~/.bashrc` або через `export`
перед запуском):

```bash
export TELEGRAM_BOT_TOKEN="1234567:AAAA..."
export EBAY_CLIENT_ID="твій-client-id"
export EBAY_CLIENT_SECRET="твій-client-secret"
export EBAY_MARKETPLACE_ID="EBAY_DE"   # або EBAY_US, EBAY_GB тощо
```

Інші параметри (поріг знижки, інтервал перевірки, стартовий список
моделей) редагуються в `config.py`.

## 4. Запуск

```bash
python main.py
```

Для постійної роботи на сервері — запусти через `screen`/`tmux` або
онови systemd-юніт (як у твоїх інших ботах), наприклад:

```ini
[Unit]
Description=eBay Flip Bot
After=network.target

[Service]
WorkingDirectory=/home/<user>/ebay_flip_bot
ExecStart=/home/<user>/ebay_flip_bot/venv/bin/python main.py
Environment=TELEGRAM_BOT_TOKEN=...
Environment=EBAY_CLIENT_ID=...
Environment=EBAY_CLIENT_SECRET=...
Environment=EBAY_MARKETPLACE_ID=EBAY_DE
Restart=always

[Install]
WantedBy=multi-user.target
```

## 4.1 Перевірка тестів

Локально запусти синтаксичну перевірку та unit-тести:

```bash
python -m py_compile main.py config.py
python -m unittest discover -s tests -v
```

У репозиторії налаштований GitHub Actions workflow, який автоматично
виконує ці перевірки для кожного push і pull request.

## 5. Команди в Telegram

- `/start` — довідка
- `/presets` — одразу додати стартовий список: PS5, PS5 Pro, Nintendo
  Switch, Switch OLED, MacBook Air M1/M2, MacBook Pro 13" M1 / 14" M1
  Pro, Dell XPS 13 (9310/9320), ThinkPad X1 Carbon (Gen 9/10)
- `/watch <запит> | <поріг%>` — додати власну модель, напр.
  `/watch AirPods Pro 2 | 30`
- `/list` — показати активні відстеження та поточну медіану ціни
- `/remove <id>` — вимкнути відстеження
- `/setthreshold <id> <%>` — змінити поріг знижки для конкретного запиту

## 6. Важливе обмеження цієї версії

eBay офіційно рахує "справжню" ринкову ціну на основі **проданих**
лотів (Marketplace Insights API), але доступ до цього API eBay видає
вибірково, за окремим запитом до партнерської підтримки. Тому в цій
версії бот рахує медіану **серед активних оголошень** (виставлені
ціни) — це трохи менш точно, бо не всі виставлені лоти продаються за
вказаною ціною, але не вимагає додаткового погодження з eBay і цілком
робоче рішення для старту.

Якщо пізніше отримаєш доступ до Marketplace Insights API — заміна
джерела даних у `ebay_client.py` займе мінімум змін, структура бота
підтримує це без переробки.

## 7. Наступні кроки, які варто розглянути

- Дедуплікація/фільтр за станом товару окремо для кожної моделі
  (зараз усі стани змішані в одному порозі)
- Врахування вартості доставки в підсумковій ціні
- Перевірка рейтингу продавця (щоб уникати шахрайських лотів)
- Кнопки "куплено/пропущено" під сповіщенням для збору статистики