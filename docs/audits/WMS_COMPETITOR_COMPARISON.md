# Store WMS у порівнянні з професійними WMS

Дата: 09.11.2026. Мета — не копіювати великі системи, а взяти лише те, що реально пришвидшить
роботу складу матеріалів під проєкти (меблі, fit-out, будівництво) і не ускладнить програму.

## Джерела і обмеження

Використано лише офіційну документацію, прочитану під час аудиту:

* Odoo 18: [Replenishment](https://www.odoo.com/documentation/18.0/applications/inventory_and_mrp/inventory/warehouses_storage/replenishment.html),
  [Putaway rules](https://www.odoo.com/documentation/18.0/applications/inventory_and_mrp/inventory/shipping_receiving/daily_operations/putaway.html),
  [Batch picking](https://www.odoo.com/documentation/18.0/applications/inventory_and_mrp/inventory/shipping_receiving/picking_methods/batch.html).
* Microsoft Dynamics 365 Supply Chain Management (Microsoft Learn):
  [Cycle counting](https://learn.microsoft.com/dynamics365/supply-chain/warehousing/cycle-counting),
  [Cycle counting scenarios](https://learn.microsoft.com/dynamics365/supply-chain/warehousing/cycle-counting-scenarios),
  [Mobile device menu items](https://learn.microsoft.com/dynamics365/supply-chain/warehousing/configure-mobile-devices-warehouse),
  [Reason codes for counting](https://learn.microsoft.com/dynamics365/supply-chain/warehousing/reason-codes-for-counting-journals).
* Zoho Inventory Help: [Picklist](https://www.zoho.com/us/inventory/help/picklist/),
  [Bin locations](https://www.zoho.com/us/inventory/help/bin-locations/),
  [Replenishments](https://www.zoho.com/us/inventory/help/items/replenishments.html),
  [Purchase receives](https://www.zoho.com/us/inventory/help/purchase-orders/purchase-receive.html),
  [Backorders](https://www.zoho.com/us/inventory/help/backorder/backorders.html).

**Не вдалося прочитати:** сторінку Odoo про підрахунок запасів (сайт забороняє автоматичне
читання) і загальний індекс довідки Zoho; також під час роботи вичерпався денний ліміт одного з
інструментів веб-читання. Тому про інвентаризацію в Odoo тут **нічого не стверджується**. Усе, що
не підтверджено цими сторінками, в таблицях позначено «не перевірено».

---

## 1. Що роблять професійні системи (лише перевірене)

| Система | Підхід (з документації) | Що це означає для нас |
|---|---|---|
| Odoo | **Reordering rules** (min/max): автоматично або вручну пропонують замовлення, коли запас ≤ мінімуму; ручні правила показуються у **replenishment report** для перевірки | У нас **уже є** (Reorder Required + чернетки PO, нічого не відправляється саме) ✅ |
| Odoo | **Make to order** — закупівля запускається після підтвердження замовлення | Аналог для нас: «To Purchase» з потреби проєкту (не автоматично, а як пропозиція) |
| Odoo | **Putaway rules** — при надходженні товар направляється в під-локацію за правилом (товар / категорія), варіанти «Last used», «Closest location» | У нас — комірка за замовчуванням на товарі. Для одного складу цього достатньо; повні правила — зайва складність |
| Odoo | **Batch picking** — один комплектувальник збирає кілька замовлень за один обхід; партії створюються вручну або за критеріями (контакт, перевізник, локація); при недоборі — backorder | Нам корисний **простий picking list** для одного проєкту/кількох юнітів; групування кількох проєктів — пізніше |
| D365 SCM | **Cycle counting**: роботи створюються за порогами або планом підрахунку; рахують з мобільного; розбіжності → «Pending review»; допуск розбіжності на працівника (% або кількість); **blind count** і повторний підрахунок (Number of attempts); **spot counting** комірки без плану; коди причин | У нас вже є blind count, затвердження, recount ✅. Корисно взяти: **сканування в підрахунку**, **план циклічного підрахунку** (без «робіт» і пулів) |
| D365 SCM | **Warehouse Management mobile app**: меню пунктів «Purchase order item receiving», «… and put away», прийом за номером PO і товаром | Корисно для майбутнього мобільного доступу: ті самі 4–5 сценаріїв (прийом за PO, видача, переміщення, підрахунок, пошук комірки) |
| Zoho | **Picklist**: створюється з одного або кількох замовлень, статуси Yet To Start / In Progress / On Hold / Completed, рекомендована комірка, групування «By Item» / «By Sales Order», призначення комплектувальнику | Найближче до потреби: **picking list з проєкту** з колонками комірка/кількість/відмітка, статус простий |
| Zoho | **Bin locations**: комірки вибираються при прийомі і видачі; переміщення між комірками; після використання комірку не можна видалити | Вже є ✅ (у нас навіть краще: архівування з історією) |
| Zoho | **Replenishments**: reorder level, max level, preferred vendor, перевірка з частотою, сповіщення, PO створюється вручну або масово | Вже є ✅; бракує **Maximum level** (P3) і сповіщення поза програмою (P2) |
| Zoho | **Purchase receive**: часткове отримання, статуси In Transit / Received; backorder — PO під замовлення клієнта, якого немає на складі | Часткові поставки є ✅. Аналог backorder для нас — «To Purchase» по проєкту |

---

## 2. Матриця порівняння

Колонка «Store WMS зараз» — лише перевірене в коді/на екрані v1.19.0.

| Можливість | Store WMS зараз | Підхід професійних систем | Рекомендація | Пріоритет |
|---|---|---|---|---|
| Пошук матеріалу | ✅ Глобальний пошук Ctrl+K + фільтри Materials, штрихкод, 50 мс на 100k | Стандарт | Додати дії Transfer/Reserve у контекстне меню; tooltip з усіма комірками | P2 |
| Стани запасу | ✅ On Hand / Reserved / Available / Incoming | Odoo/Zoho — аналогічно | Без змін | — |
| Планування матеріалів проєкту | ⚠️ Дані є (Required/Reserved/Issued/Returned/Remaining), але немає «що купити» і дій | Odoo MTO; Zoho backorder | **Project Material Planning** view + To Purchase | **P1** |
| Резервування | ✅ по одному товару, з захистом від перерезерву | Odoo/Zoho резервують по документу | **Масовий резерв** «Reserve remaining» | **P1** |
| Picking lists | ❌ немає (лише Issue note після проведення) | Zoho Picklist, Odoo batch picking | Picking list з проєкту/юнітів, PDF + сканування | **P1** |
| Видача під проєкт | ✅ ручні рядки | Видача з документа-джерела | «Load from project / from picking list» | **P1** |
| Прийом за PO | ✅ лінії з PO, часткові, проблеми | D365 прийом за PO+товар | Сканування номера PO; карантин для пошкоджених у тому ж документі | P2 |
| Розміщення (putaway) | Комірка за замовчуванням | Odoo putaway rules | Лише підказка «останні комірки цього товару» | P3 |
| Переміщення | ✅ 1 товар за документ | Zoho Transfer/Move orders з кількома рядками | Масовий Transfer / «перемістити комірку» | P2 |
| Інвентаризація | ✅ blind, recount, approve | D365 cycle count плани, пороги, допуски | Сканування в підрахунку; план циклічних підрахунків; допуск без перевірки | P2 |
| Дозамовлення | ✅ Reorder level + чернетки PO | Odoo reordering rules, Zoho replenishments | Додати Max level (P3), щоденне сповіщення (P2) | P2/P3 |
| Закупівлі | ✅ PO-цикл, overdue | Стандарт | PO email/PDF постачальнику; дата на рядку; Supplier performance | P2 |
| Вихідні доставки / кур'єр | ❌ | Odoo/D365 — відвантаження і перевізники (деталі не перевірялись) | Простий **Delivery booking**: адреса з проєкту, слот, авто, палети, вага, лист кур'єру, POD | P2 |
| Мобільне сканування | ❌ (є сканер-клавіатура на ПК ✅) | D365 Warehouse Management mobile app | Phase G: веб/мобільний клієнт після PostgreSQL | P3 (зараз) |
| Багато користувачів одночасно | ❌ (один ПК; `BEGIN IMMEDIATE`) | Серверні БД | PostgreSQL + тести на ньому | P1 до розширення |
| Звіти | ✅ 13 звітів | Odoo/Zoho — аналітика, дашборди | Outstanding, Supplier performance, Stocktake differences, Consumption | P2 |
| Аудит і незмінність | ✅ журнал + тригери, сторно | Стандарт великих систем | Без змін (сильна сторона) | — |
| Бекапи | ✅ розклад, test restore | У хмарних — постачальник | Без змін | — |
| Облік (Xero) | ❌ | Інтеграції з бухгалтерією | Phase G (див. дорожню карту, розділ Xero) | P3 |

---

## 3. Варто перейняти

1. **Picking list з документа-джерела** (Zoho Picklist, Odoo batch picking) — у нас джерело = проєкт / юніти / рівень.
2. **Простий статус підготовки** (Yet to start → In progress → Completed), без «робіт», пулів і хвиль.
3. **Цикл «потреба → резерв → видача»** у кілька кліків, з чітким «що купити».
4. **Сканування в інвентаризації і spot count комірки** (D365) — без мобільного застосунку, на ПК зі сканером.
5. **Допуск розбіжності** (D365): дрібна розбіжність у межах % не потребує окремого перегляду (за налаштуванням).
6. **Пропозиції замість автоматики** (Odoo manual reordering rules, Zoho «створіть PO вручну»): система
   показує, людина підтверджує — це вже ваш принцип, зберегти його для всіх нових автоматизацій.

## 4. Що НЕ варто переймати (зайва складність)

| Функція | Чому ні |
|---|---|
| License plates, контейнери, хвилі (D365) | Розраховано на розподільчі центри; у вас один склад і матеріал під проєкти |
| Work pools, system-directed work, пріоритети робіт (D365) | Потребує окремого диспетчера і мобільних терміналів у кожного |
| Багатокрокові маршрути (Odoo multi-step routes: input → quality → stock) | Подвоює кількість документів; карантинна комірка вирішує ту саму задачу |
| Повні putaway rules | Комірки за замовчуванням достатньо |
| MPS (Odoo) | Немає прогнозів продажів у WMS |
| Візуальна 3D/2D карта складу | Декоративно; для одного складу пошук комірки за кодом швидший |
| Повний MRP/BOM-виробництво | Окремий продукт; Finished Products покриває поточну потребу |
| Бухгалтерія в WMS | Залишити Xero джерелом фінансів |
