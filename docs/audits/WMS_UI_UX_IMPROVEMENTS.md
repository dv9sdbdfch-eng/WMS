# Store WMS — покращення UI/UX

Дата: 09.11.2026, версія v1.19.0. Основа — 25 знімків екранів 1366×768, знятих у цьому аудиті,
і код `wms/ui/`. Попередні макети редизайну (`/mnt/project-files/wms/redesign/mockups.html`,
31.10.2026) **не дублюються**: де пропозиція збігається з ними — є посилання «як у макетах».

**Що зберігаємо обов'язково (ваші рішення):** окремі колонки ієрархії в Projects; клікабельні
заголовки Project Manager і Project; ручне розгортання; згортувана панель деталей; світло-зелене
виділення активного проєкту (#E8F5E9); окреме виділення клітинки (рамка #2F6FB2); Dashboard як
робочий простір із таблицею матеріалів (v1.14); фільтри та пошук не запам'ятовуються між
сеансами (ваше правило v1.14) — запам'ятовуються лише колонки, сортування, панелі.

---

## 1. Навігація

| # | Проблема (перевірено) | Пропозиція | Пріоритет |
|---|---|---|---|
| N1 | «Deliveries» = лише вхідні накладні | Перейменувати в **«Supplier Deliveries»**; вихідні доставки (коли з'являться) — «Outgoing Deliveries» у PROJECTS | P2 |
| N2 | Returns і Stocktake сховані в згорнутому MORE, хоча це щоденні складські операції | OPERATIONS: Goods In · Goods Out · Transfer · **Returns**; INVENTORY: **Stocktake** · Reorder Required | P2 |
| N3 | Transfer у боковому меню дублює F4 (D5 з попереднього аудиту, ще відкрите) | Прибрати з меню або лишити — **ваше рішення**; рекомендую лишити (новачки не знають F4) | P3 |
| N4 | Reservations — прихований екран | Доступ з Projects/Planning і контекстного меню Materials; сам екран лишити як журнал | P2 |
| N5 | Немає «хлібних крихт» між пов'язаними записами | У вікнах документів — посилання: PO ↔ накладна ↔ проблема поставки ↔ матеріал ↔ проєкт (частково вже є) | P3 |
| N6 | Клавіші: F2/F3/F4, Ctrl+K, Ctrl+B, F11, F5, Ctrl+D ✅ | Додати **Ctrl+F** = фільтр поточної таблиці (як у макетах); Ctrl+Shift+R = Reserve на Planning | P3 |

## 2. Dashboard

Залишаємо структуру «Dashboard (згорнуто) → Action Required → таблиця матеріалів».

| # | Пропозиція | Пріоритет |
|---|---|---|
| D1 | Новий рядок **PROJECTS**: «Projects short of material (N)», «Reservations due in 7 days (N)», «Over-issued (N)» → кожен відкриває Planning з фільтром | P1 |
| D2 | Рядок **TODAY**: «Deliveries expected today/this week» (за expected_date PO), «Open drafts» (Goods In), «Open stocktakes» — дані вже рахуються в `DashboardData.today` | P2 |
| D3 | DATA QUALITY згорнути за замовчуванням в одну кнопку «Data quality (24)» — менше шуму | P2 |
| D4 | Лічильники 0 показувати сірим (вже так) — залишити | — |

## 3. Таблиці (єдиний стандарт)

Зараз дві реалізації: нова `MaterialsView` (колонки, види, ледаче завантаження) і старі
`ListPage`-таблиці (Goods Out History, Deliveries, PO, Returns, Finished, Reorder…).

| Стандарт | Зараз | Ціль |
|---|---|---|
| Висота рядка | 28 px (компактний Dashboard), 40 px з фото | 28 px за замовчуванням, 40 px лише коли видно Photo |
| Ширина колонок | Materials: Status обрізана при 1366 px | Автопідбір + «Fit columns» у меню Columns; довгий текст — «…» з tooltip |
| Видимість/порядок колонок | ✅ лише MaterialsView | Для всіх списків (спільний компонент) |
| Сортування | ✅ | ✅ скрізь, запам'ятовувати |
| Фільтри | Різні рядки фільтрів | Однаковий рядок: Пошук · 3–4 головні фільтри · More filters · Clear |
| Кількість рядків | «1000 record(s)» без попередження | «Showing first 1 000 of 3 412 — narrow the dates» або ледаче завантаження як у Materials |
| Вибір | Галочки + Ctrl/Shift | Однаково скрізь; панель масових дій з'являється при виборі ✅ (Materials) |
| Контекстне меню | Materials: Open, Goods In/Out, Copy | + Transfer…, Reserve…, Print label, Open bin, Open project |
| Числа | Праве вирівнювання ✅, роздільник тисяч ✅ | Від'ємні — червоним ✅; нулі — сірим |

## 4. Форми і діалоги

| # | Проблема | Пропозиція | Пріоритет |
|---|---|---|---|
| F1 | Захист незбережених змін лише на панелі Projects (v1.19) | Той самий механізм (знімок значень → Save/Discard/Cancel) для діалогів Material, Project, PO, Supplier і форм Goods In/Out/Returns при закритті програми | P1 |
| F2 | Обов'язкові поля позначені «*» ✅ | Залишити; помилку показувати біля поля (зараз — вікно) | P3 |
| F3 | Goods In: область скану займає праву половину, таблиця рядків обрізана | Скан — у згортувану праву панель (як Details у Projects), за замовчуванням вузька мініатюра | P1 |
| F4 | Goods Out: 6 полів шапки займають ⅓ висоти | Шапка у 2 колонки (Project · Date / Requested by · Job ref), Notes — згорнуте | P2 |
| F5 | Списки вибору товару завантажують усі матеріали (2,5 с на 50k) | Пошук під час введення (перші 50 збігів з БД) | P1 |
| F6 | Enter/Tab порядок для сканера ✅ (Goods In/Out, Transfer) | Додати в Stocktake і Returns | P2 |

## 5. Нові екрани

### 5.1 Project Material Planning (вкладка у Projects)

Перемикач над деревом: **Structure** (як зараз) | **Planning**. Planning — таблиця MaterialsView
з `fixed_site_id` (компонент уже є) з колонками:

`Code · Material · Required · Net Issued · Remaining · Reserved · To Allocate · Available · Incoming · To Purchase · Status`

* Статус-бейдж рядка: **Complete** (Remaining = 0) · **Reserved** (To Allocate = 0) · **Available**
  (можна зарезервувати зі складу) · **On order** (покривається Incoming) · **To purchase**.
* Область: увесь проєкт / вибраний блок / рівень / юніт (з дерева).
* Дії: Reserve remaining… · Picking list… · Issue… · Create draft POs… · Export.
* Назви колонок — після вашого рішення РІШ-1 (див. дорожню карту).

### 5.2 Picking list (діалог + PDF)

Джерело (проєкт/область або резерви) → таблиця рядків з комірками в порядку обходу → PDF A4 →
«Issue from this list» відкриває Goods Out із заповненими рядками.

### 5.3 Outgoing delivery (діалог)

Шапка з проєкту (адреса, контакт), дата/слот, кур'єр, авто, палети, вага, статус, POD-вкладення,
кнопка «Email courier» (чернетка).

## 6. Візуальна узгодженість і дизайн-система

Зберігаємо ідентичність: темно-синє меню (#1F3A5F), синій акцент (#2F6FB2), світле тло
(#F5F7FA). Макети 31.10 пропонували світло-сіре меню — **не змінюємо без вашого рішення**.

### 6.1 Кольори (токени, вже частково в `theme.py`)

| Токен | Значення | Використання |
|---|---|---|
| `primary` | #1F3A5F | Бокове меню, заголовки |
| `accent` | #2F6FB2 | Основні кнопки, фокус, рамка клітинки |
| `accent-soft` | #D6E6F7 | Виділення рядка |
| `project-active` | #E8F5E9 | Активний проєкт (зберегти) |
| `success` / `success-soft` | #067647 / #ECFDF3 | In Stock, Complete |
| `warning` / `warning-soft` | #B54708 / #FFFAEB | Low, Reorder, Partial |
| `danger` / `danger-soft` | #B42318 / #FEF3F2 | Out, Negative, Overdue |
| `info` / `info-soft` | #175CD3 / #EFF8FF | Підказки, On order |
| `text` / `muted` / `border` | #101828 / #667085 / #D0D5DD | Текст, другорядний, рамки |
| `bg` / `surface` | #F5F7FA / #FFFFFF | Тло, картки/таблиці |

**Дія:** перенести 24 кольори, записані прямо у віджетах (`wms/ui/**` поза `theme.py`), у токени —
тоді будь-яка зміна кольору робиться в одному місці (P2).

### 6.2 Типографіка

Segoe UI (Windows), 10 pt основний текст · 9 pt підказки (`muted`) · 11 pt semi-bold заголовок
секції · 15 pt semi-bold заголовок екрана · моноширинний не потрібен. Підзаголовок екрана — **один
рядок**; довгі інструкції → кнопка «?» з підказкою (P2).

### 6.3 Кнопки

| Тип | Вигляд | Правило |
|---|---|---|
| Primary | Заливка accent, білий текст | **Одна** на екран/діалог (Post, Save) |
| Secondary | Біла, рамка border | Решта дій |
| Danger | Біла, червоний текст | Delete, Cancel order, Reverse — завжди з підтвердженням і причиною |
| Tool | Без рамки, іконка | На панелях таблиць |

Висота 30 px, радіус 6 px (зараз так ✅).

### 6.4 Статус-бейджі

Однаковий вигляд для запасу, PO, резервів, проєктів: кольоровий текст на «soft»-тлі, 11 px радіус.
Набори: Stock (In Stock / Low / Reorder / Out) · PO (Draft / Ordered / Partial / Received / Cancelled
/ Overdue) · Reservation (Active / Fulfilled / Released) · Planning (Complete / Reserved / Available
/ On order / To purchase).

### 6.5 Іконки

Зараз іконок немає (символи ☰ ▸ ⇄ − +). Рекомендація з макетів: набір **Lucide** (ліцензія ISC,
SVG вкладаються в програму, без нової залежності) — лише для меню, кнопок дій у панелях і статусів.
P3.

### 6.6 Відступи і щільність

Сітка 4 px: 8 px між полями, 12 px між секціями, 16 px поля сторінки. Без великих карток: лічильники
Dashboard — компактні «плитки» в один рядок (вже так ✅).

### 6.7 Панелі

Однакова поведінка для всіх бічних панелей (деталі проєкту, скан накладної, фільтри): кнопка ▸/◂ на
краю таблиці, ширина регулюється роздільником, стан запам'ятовується (`QSettings layout/...`) — як
уже зроблено в Projects (v1.18).

### 6.8 Стани виділення

Рядок: `accent-soft`. Клітинка: рамка 2 px `accent` (як Projects). Активний проєкт: `project-active`
під рядками (зберегти). Рядок з проблемою (Low/Out): `danger-soft` (вже так у Materials ✅).

### 6.9 Помилки і порожні стани

* Помилки перевірки — текстом біля поля + один рядок зверху, не модальне вікно (P3).
* Системні помилки — вікно з коротким текстом, кнопкою «Copy details» і записом у журнал (є ✅).
* Порожня таблиця — одне речення «що зробити»: «No materials on this project yet — Import Excel or
  Add Material» з кнопкою (P3). Приклад правильного: Projects «Select a manager, project…» ✅.

---

## 7. Пропозиції по екранах

| Екран | Що змінити | Пріоритет |
|---|---|---|
| Dashboard | D1–D3 вище | P1/P2 |
| Materials | Fit columns при 1366; контекстне меню + Transfer/Reserve/Label; tooltip комірок | P2 |
| Locations & Bins | «Move whole bin…», фільтр «порожні комірки» | P2 |
| Goods In | Скан — згортувана панель; колонка Damaged qty; таблиця на всю ширину | P1/P2 |
| Goods Out | Load from project / picking list; компактна шапка; Requirements-підказка «Remaining after this issue» | P1 |
| Transfer | Кілька рядків; «Move whole bin» | P2 |
| Projects | Перемикач Structure/Planning; компактний підзаголовок; дії Reserve/Pick/Issue в меню рядка; на 1366 px за замовчуванням вужчі колонки Block/Level | P1 |
| Purchase Orders | Email draft з PDF; колонка Days overdue; ледаче завантаження | P2 |
| Supplier Deliveries | Перейменування; ледаче завантаження | P2 |
| Returns | Return from project з Net Issued; DAMAGED → карантин | P0/P2 |
| Reservations | Журнал + фільтр «due in 7 days» | P2 |
| Stocktake | Поле Scan; ледаче завантаження списку | P2 |
| Finished Products | Перевести на MaterialsView (D3 з попереднього аудиту) | P3 |
| Reports | Нові звіти; «Export…» на кожному списку відкриває відповідний звіт із фільтрами (D7) | P2 |
| Users / Audit / Settings | Політика паролів, автовихід; експорт Audit Log за період | P1/P3 |
