"""Small runtime localization layer for the Qt front-end.

The backends deliberately keep their machine-readable/CLI output in English.  This
module localizes application-owned GUI chrome and status text while leaving Nexus
mod-authored content (names, descriptions, changelogs) untouched.
"""
from __future__ import annotations

import locale
import re
import sys
from typing import Callable

LANG_EN = "en"
LANG_UK = "uk"
SUPPORTED_LANGUAGES = (LANG_EN, LANG_UK)
LANGUAGE_NAMES = {LANG_EN: "English", LANG_UK: "Українська"}
_CURRENT_LANGUAGE = LANG_EN
_MANAGER = None


def normalize_language(value: object) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    if raw.startswith("uk") or raw in {"ua", "ukrainian", "українська", "українська мова"}:
        return LANG_UK
    return LANG_EN


def system_language() -> str:
    try:
        raw = locale.getlocale()[0] or locale.getdefaultlocale()[0] or ""
    except Exception:
        raw = ""
    return normalize_language(raw)


# Exact application-owned strings.  External Nexus content is intentionally not in
# this catalogue, so author text is never machine-translated by the manager.
UK: dict[str, str] = {
    # shared/common
    "Language": "Мова",
    "Theme": "Тема",
    "Theme:": "Тема:",
    "Qt Default": "Qt За замовчуванням",
    "Qt Dark": "Qt Темна",
    "Qt White": "Qt Біла",
    "Cyberpunk 2077": "Cyberpunk 2077",
    "Українська": "Українська",
    "Nexus Collection Manager — Cyberpunk 2077": "Nexus Collection Manager — Cyberpunk 2077",
    "ID": "ID",
    "FOMOD": "FOMOD",
    "SHA-256": "SHA-256",
    "<game>/downloaded_mods/": "<game>/downloaded_mods/",
    "<game>/downloaded_mods/archives": "<game>/downloaded_mods/archives",
    "nxm://cyberpunk2077/mods/123/files/456?key=…&expires=…&user_id=…": "nxm://cyberpunk2077/mods/123/files/456?key=…&expires=…&user_id=…",
    "Language:": "Мова:",
    "English": "English",
    "Ukrainian": "Українська",
    "Setup": "Налаштування",
    "Guided Setup…": "Майстер налаштування…",
    "Setup saved": "Налаштування збережено",
    "Guided setup is complete. Restart Nexus Collection Manager to reload every open tab with the new paths/account settings.": "Майстер налаштування завершено. Перезапустіть Nexus Collection Manager, щоб усі відкриті вкладки підхопили нові шляхи й параметри облікового запису.",
    "Refresh": "Оновити",
    "Refresh status": "Оновити стан",
    "Stop": "Зупинити",
    "Retry": "Повторити",
    "Cancel": "Скасувати",
    "Yes": "Так",
    "No": "Ні",
    "OK": "Гаразд",
    "&Yes": "&Так",
    "&No": "&Ні",
    "&OK": "&Гаразд",
    "< &Back": "< &Назад",
    "&Next >": "&Далі >",
    "&Finish": "&Завершити",
    "Close": "Закрити",
    "Delete": "Видалити",
    "Delete selected": "Видалити вибране",
    "Install": "Встановити",
    "Installed": "Встановлено",
    "Uninstall": "Видалити мод",
    "Enable": "Увімкнути",
    "Disable": "Вимкнути",
    "Enabled": "Увімкнено",
    "Disabled": "Вимкнено",
    "Active": "Активний",
    "State": "Стан",
    "Status": "Стан",
    "Progress": "Прогрес",
    "Error": "Помилка",
    "Size": "Розмір",
    "File": "Файл",
    "Files": "Файли",
    "Type": "Тип",
    "Version": "Версія",
    "Uploaded": "Завантажено",
    "Created": "Створено",
    "Category": "Категорія",
    "Item": "Елемент",
    "Details": "Деталі",
    "Detail": "Деталі",
    "Description": "Опис",
    "Requirements": "Вимоги",
    "Changelog": "Журнал змін",
    "DLC": "DLC",
    "None": "Немає",
    "Reason": "Причина",
    "Evidence": "Докази",
    "Confidence": "Впевненість",
    "Review": "Перевірка",
    "Safety": "Безпека",
    "Destination": "Призначення",
    "Resolution": "Рішення",
    "Current owner": "Поточний власник",
    "Current": "Поточна",
    "Latest": "Найновіша",
    "Source path": "Шлях джерела",
    "Source archive": "Архів-джерело",
    "Original source": "Початкове джерело",
    "Entry": "Запис",
    "Archive": "Архів",
    "Nexus": "Nexus",
    "Nexus ID": "Nexus ID",
    "Mod": "Мод",
    "Mods": "Моди",
    "Profile": "Профіль",
    "Profiles": "Профілі",
    "Framework": "Фреймворк",
    "Drift": "Відхилення",
    "Layer": "Шар",
    "Severity": "Серйозність",
    "Conflict": "Конфлікт",
    "Review": "Перевірка",
    "Ready.": "Готово.",
    "aria2c ✓": "aria2c ✓",
    "aria2c ✗ (download needs it)": "aria2c ✗ (потрібен для завантаження)",
    "bsdtar ✓": "bsdtar ✓",
    "bsdtar ✗ (install needs it)": "bsdtar ✗ (потрібен для встановлення)",
    "Idle.": "Очікування.",
    "Starting…": "Запуск…",
    "Stopping…": "Зупинка…",
    "Done.": "Готово.",
    "Missing": "Відсутнє",
    "Browse…": "Огляд…",
    "show": "показати",
    "Top": "На початок",
    "Bottom": "У кінець",
    "↑ Up": "↑ Вище",
    "↓ Down": "↓ Нижче",
    "Left": "Ліворуч",
    "Right": "Праворуч",
    "Left:": "Ліворуч:",
    "Right:": "Праворуч:",
    "Before:": "Перед:",
    "After:": "Після:",
    "Before (wins first)": "Перед (має пріоритет)",
    "After": "Після",
    "Open Nexus": "Відкрити Nexus",
    "Open Nexus Mods": "Відкрити Nexus Mods",
    "Open Nexus page": "Відкрити сторінку Nexus",
    "Mod details…": "Деталі мода…",
    "Game root": "Коренева папка гри",
    "Game root:": "Коренева папка гри:",
    "Downloads:": "Завантаження:",
    "Downloads folder:": "Папка завантажень:",
    "Download folder:": "Папка завантажень:",
    "Cookies file:": "Файл cookies:",
    "Jobs:": "Завдання:",
    "Port:": "Порт:",
    "Account:": "Обліковий запис:",
    "API key:": "API-ключ:",
    "Nexus API key:": "API-ключ Nexus:",
    "Mod ID:": "ID мода:",
    "File ID:": "ID файла:",
    "Scan roots:": "Корені сканування:",
    "Hash workers:": "Потоки хешування:",
    "Hash jobs:": "Завдання хешування:",
    "Limits:": "Обмеження:",
    "Collection(s):": "Колекції:",
    "Downloads / archives:": "Завантаження / архіви:",
    "Content lookups:": "Пошуки за вмістом:",
    "Fuzzy searches:": "Нечіткі пошуки:",
    "Selectors:": "Селектори:",
    "Parallel extract:": "Паралельне розпакування:",
    "Connections/file:": "З’єднань/файл:",
    "Detected:": "Знайдено:",
    "Managed staging:": "Керований staging:",
    "Archive library:": "Бібліотека архівів:",
    "Active / target profile:": "Активний / цільовий профіль:",
    "Inbox filter:": "Фільтр вхідних:",
    "ε:": "ε:",
    "Min samples:": "Мін. вибірок:",
    "TF-IDF + DBSCAN:": "TF-IDF + DBSCAN:",
    # main tabs
    "1 · Downloads": "1 · Завантаження",
    "2 · Trending": "2 · Популярні",
    "3 · Endorsed": "3 · Схвалені",
    "4 · Tracked": "4 · Відстежувані",
    "5 · Updates": "5 · Оновлення",
    "6 · Collections": "6 · Колекції",
    "7 · Install": "7 · Встановлення",
    "8 · Deploy": "8 · Розгортання",
    "9 · Profiles": "9 · Профілі",
    "10 · Conflicts": "10 · Конфлікти",
    "11 · Load Order": "11 · Порядок завантаження",
    "12 · Archives": "12 · Архіви",
    "13 · Manage": "13 · Керування",
    "14 · Scanner": "14 · Сканер",
    "15 · Nexus": "15 · Nexus",
    "16 · Correlate": "16 · Зіставлення",
    "17 · Advanced": "17 · Розширене",
    # onboarding
    "1 · Connect Nexus Mods": "1 · Підключення Nexus Mods",
    "Validate your Nexus account so downloads, updates, endorsements, tracking and metadata work immediately.": "Перевірте обліковий запис Nexus, щоб одразу працювали завантаження, оновлення, схвалення, відстеження та метадані.",
    "Nexus Mods Personal API Key": "Особистий API-ключ Nexus Mods",
    "Show key": "Показати ключ",
    "Continue without Nexus login": "Продовжити без входу в Nexus",
    "Local install/deploy/profile features still work. You can add a key later.": "Локальне встановлення, розгортання та профілі продовжать працювати. Ключ можна додати пізніше.",
    "Validate account": "Перевірити обліковий запис",
    "Open Nexus API Access": "Відкрити Nexus API Access",
    "Enter your Nexus Mods Personal API Key, then validate it.": "Введіть особистий API-ключ Nexus Mods і перевірте його.",
    "Validating Nexus account…": "Перевірка облікового запису Nexus…",
    "Nexus login": "Вхід у Nexus",
    "Paste your Nexus Mods Personal API Key first.": "Спочатку вставте особистий API-ключ Nexus Mods.",
    "Offline mode selected. Nexus-backed features can be enabled later by adding an API key.": "Вибрано офлайн-режим. Функції Nexus можна ввімкнути пізніше, додавши API-ключ.",
    "Key changed — validate the account again.": "Ключ змінено — перевірте обліковий запис ще раз.",
    "Could not validate this Nexus API key. Check the key and network connection.": "Не вдалося перевірити цей API-ключ Nexus. Перевірте ключ і мережеве з’єднання.",
    "2 · Configure Cyberpunk 2077": "2 · Налаштування Cyberpunk 2077",
    "Choose the actual game root. Smart Deployment will keep canonical mod content in downloaded_mods/ under this install.": "Виберіть справжню кореневу папку гри. Smart Deployment зберігатиме основний вміст модів у downloaded_mods/ всередині цього встановлення.",
    "Auto-detect Linux install": "Автовизначення встановлення Linux",
    "Validate game": "Перевірити гру",
    "Select the Cyberpunk 2077 folder containing bin/, archive/, r6/, and engine/.": "Виберіть папку Cyberpunk 2077, що містить bin/, archive/, r6/ та engine/.",
    "Staging: —": "Staging: —",
    "Looking in Steam libraries…": "Пошук у бібліотеках Steam…",
    "Validating game layout…": "Перевірка структури гри…",
    "Game path changed — validate it again.": "Шлях до гри змінено — перевірте його ще раз.",
    "Game configuration": "Налаштування гри",
    "Choose the Cyberpunk 2077 game root first.": "Спочатку виберіть кореневу папку Cyberpunk 2077.",
    "No Steam install was detected automatically. Browse to the Cyberpunk 2077 folder manually.": "Не вдалося автоматично знайти встановлення Steam. Виберіть папку Cyberpunk 2077 вручну.",
    "Game validation failed. Check the selected path.": "Перевірка гри не пройшла. Перевірте вибраний шлях.",
    "3 · Initial mod scan": "3 · Початкове сканування модів",
    "Discover existing mod files, group them with TF-IDF + DBSCAN, and record an integrity baseline before management begins.": "Знайдіть наявні файли модів, згрупуйте їх за допомогою TF-IDF + DBSCAN і створіть базову контрольну точку цілісності перед початком керування.",
    "Run initial scan": "Запустити початкове сканування",
    "Run the initial scan to finish setup.": "Запустіть початкове сканування, щоб завершити налаштування.",
    "Scanning existing mod files…": "Сканування наявних файлів модів…",
    "Initial scan": "Початкове сканування",
    "Configure at least one scan root.": "Налаштуйте щонайменше один корінь сканування.",
    "Game root changed. Run a new initial scan.": "Кореневу папку гри змінено. Запустіть нове початкове сканування.",
    "✓ Initial scan complete.": "✓ Початкове сканування завершено.",
    "Stopping scan…": "Зупинка сканування…",
    "Nexus Collection Manager · Guided Setup": "Nexus Collection Manager · Майстер налаштування",
    # downloader
    "Nexus Mods API key": "API-ключ Nexus Mods",
    "Nexus API key": "API-ключ Nexus",
    "Download": "Завантажити",
    "Nexus Mods": "Nexus Mods",
    "Premium: enter Mod ID + File ID for direct API downloads. Free: click “Download with Manager” on Nexus, or paste its nxm:// link here. Completed downloads are imported into the Archives staging library automatically.": "Premium: введіть Mod ID + File ID для прямого завантаження через API. Для безкоштовного акаунта натисніть «Download with Manager» на Nexus або вставте nxm:// посилання сюди. Завершені завантаження автоматично імпортуються до staging-бібліотеки Archives.",
    "Premium accounts can download directly. Free accounts use authorization from an NXM manager link.": "Premium-акаунти можуть завантажувати напряму. Безкоштовні акаунти використовують авторизацію з NXM-посилання менеджера.",
    "Account not checked.": "Обліковий запис не перевірено.",
    "Check account": "Перевірити обліковий запис",
    "Nexus Download Manager": "Менеджер завантажень Nexus",
    "Register Linux NXM handler": "Зареєструвати обробник NXM у Linux",
    "Make browser “Download with Manager” nxm:// links open this manager backend on Linux.": "Зробити так, щоб браузерні посилання nxm:// «Download with Manager» відкривали цей менеджер у Linux.",
    "Download / Resume": "Завантажити / Продовжити",
    "Run queue": "Запустити чергу",
    "Pause/Stop": "Пауза/Стоп",
    "Remove from queue": "Прибрати з черги",
    "delete partial on remove": "видаляти частковий файл при прибиранні",
    "Linux nxm:// handler registered for this game root.": "Обробник nxm:// у Linux зареєстровано для цієї гри.",
    "NXM handler": "Обробник NXM",
    "NXM link": "NXM-посилання",
    "Paste a complete nxm:// Download with Manager link.": "Вставте повне nxm:// посилання Download with Manager.",
    "Download queue is empty.": "Черга завантажень порожня.",
    "No API key configured": "API-ключ не налаштовано",
    "Add your Nexus Mods API key before downloading.": "Додайте API-ключ Nexus Mods перед завантаженням.",
    "Select a queued download first.": "Спочатку виберіть завантаження в черзі.",
    "Stopping after the current network read; partial bytes remain resumable…": "Зупинка після поточного мережевого читання; частково завантажені дані можна буде продовжити…",
    "Stop the active download before removing it.": "Зупиніть активне завантаження перед видаленням із черги.",
    "Remove download": "Прибрати завантаження",
    "Download Manager": "Менеджер завантажень",
    "Gentle (dodge free-download throttle)": "Щадний режим (обхід обмеження безкоштовних завантажень)",
    "Verify archives first (prune corrupt)": "Спочатку перевіряти архіви (видаляти пошкоджені)",
    "Include optional files": "Включати необов’язкові файли",
    "Collection (slug or URL):": "Колекція (slug або URL):",
    "Concurrency (0 = script default):": "Паралельність (0 = типове значення скрипту):",
    "Preview collection": "Переглянути колекцію",
    "Start download": "Почати завантаження",
    "Collection download (nexus_core.downloader + aria2c)": "Завантаження колекції (nexus_core.downloader + aria2c)",
    "Preview failed": "Попередній перегляд не вдався",
    "Preview failed — see log.": "Попередній перегляд не вдався — див. журнал.",
    "Stopping (leaving resumable partials)…": "Зупинка (часткові файли залишаться для продовження)…",
    "Download finished. Re-run to retry any failures.": "Завантаження завершено. Запустіть ще раз, щоб повторити невдалі файли.",
    "Stopped. Completed files are kept; partials resume on re-run.": "Зупинено. Завершені файли збережено; часткові продовжаться при повторному запуску.",
    # scanner
    "One root per line. Relative paths are resolved under the game root; absolute paths are allowed.": "Один корінь у рядку. Відносні шляхи визначаються від кореня гри; абсолютні шляхи дозволені.",
    "DBSCAN cosine-distance threshold. Lower = stricter grouping; 0.65 means cosine similarity >= 0.35.": "Поріг косинусної відстані DBSCAN. Менше значення = суворіше групування; 0.65 означає косинусну схожість >= 0.35.",
    "DBSCAN minimum neighborhood size. 1 keeps legitimate one-file mods as singleton groups.": "Мінімальний розмір околу DBSCAN. Значення 1 зберігає коректні однофайлові моди як окремі групи.",
    "Parallel SHA-256 integrity + MD5 Nexus-identity hashing workers.": "Паралельні потоки хешування SHA-256 для цілісності + MD5 для ідентифікації Nexus.",
    "Every file is SHA-256 hashed for integrity and MD5 hashed for Nexus identity lookup. The snapshot is stored outside the game folder; Verify compares against it without replacing the baseline.": "Для кожного файла обчислюється SHA-256 для контролю цілісності та MD5 для пошуку ідентичності Nexus. Знімок зберігається поза папкою гри; перевірка порівнює з ним, не замінюючи базову лінію.",
    "Scan + update snapshot": "Сканувати + оновити знімок",
    "Verify against snapshot": "Перевірити за знімком",
    "Recursive Mod Scanner": "Рекурсивний сканер модів",
    "No scan roots": "Немає коренів сканування",
    "Add at least one scan root.": "Додайте щонайменше один корінь сканування.",
    "Scan complete.": "Сканування завершено.",
    "Mod integrity changed": "Цілісність мода змінилася",
    "Scanner completed with errors": "Сканер завершив роботу з помилками",
    # Nexus/account/discovery
    "Nexus Mods API key (required for endorse/track)": "API-ключ Nexus Mods (потрібен для схвалення/відстеження)",
    "Used only for authenticated Nexus API requests. It is passed to the backend via environment, not shown in the command log.": "Використовується лише для автентифікованих запитів Nexus API. Передається бекенду через середовище й не показується в журналі команд.",
    "Endorsements and tracking are Nexus account state. Buttons update optimistically, then verify the live API response; failures roll back immediately.": "Схвалення та відстеження — це стан облікового запису Nexus. Кнопки оновлюються оптимістично, потім перевіряють живу відповідь API; у разі помилки зміна одразу відкочується.",
    "Nexus account — installed mods": "Обліковий запис Nexus — встановлені моди",
    "Sync with Nexus": "Синхронізувати з Nexus",
    "Loading installed Nexus mods…": "Завантаження встановлених модів Nexus…",
    "Add your Nexus Mods API key to sync endorsement/tracking state.": "Додайте API-ключ Nexus Mods для синхронізації схвалень і відстеження.",
    "Add your Nexus Mods API key first.": "Спочатку додайте API-ключ Nexus Mods.",
    "Free account: Nexus requires Download with Manager authorization.": "Безкоштовний акаунт: Nexus вимагає авторизацію Download with Manager.",
    "Download with Manager required": "Потрібен Download with Manager",
    "Nexus free accounts require a fresh Download with Manager authorization. Open this mod's Files page now?": "Для безкоштовних акаунтів Nexus потрібна нова авторизація Download with Manager. Відкрити сторінку Files цього мода зараз?",
    "Nexus discovery": "Пошук модів Nexus",
    "Refresh from Nexus": "Оновити з Nexus",
    "Trending": "Популярні",
    "Recently Updated": "Нещодавно оновлені",
    """Nexus free accounts require a fresh Download with Manager authorization. Open this mod's Files page now?

After clicking Download with Manager there, the registered NXM handler will send the archive to Downloads.""": """Для безкоштовних акаунтів Nexus потрібна нова авторизація Download with Manager. Відкрити сторінку Files цього мода зараз?

Після натискання Download with Manager зареєстрований NXM-обробник передасть архів у Завантаження.""",
    "Endorsed": "Схвалені",
    "Tracked": "Відстежувані",
    "Premium: download + install the primary/newest MAIN file immediately using FOMOD defaults and safe conflict skipping. Free accounts are sent to Nexus for the required Download-with-Manager authorization.": "Premium: одразу завантажити й встановити основний/найновіший MAIN-файл з типовими параметрами FOMOD і безпечним пропуском конфліктів. Безкоштовний акаунт буде перенаправлено до Nexus для обов’язкової авторизації Download with Manager.",
    "Discovery is live from Nexus Mods. Install uses the existing Download Manager → Archives → Smart Installer pipeline. Unattended installs use authored FOMOD defaults and Skip for existing-file conflicts.": "Список завантажується наживо з Nexus Mods. Встановлення використовує наявний ланцюжок Download Manager → Archives → Smart Installer. Автоматичне встановлення використовує задані автором типові параметри FOMOD та Skip для конфліктів із наявними файлами.",
    # manage/profile
    "Park folder:": "Папка паркування:",
    "Note: nexus_core.modctl has its game-root/park-dir baked in; these fields only drive the table below, so keep them matching.": "Примітка: nexus_core.modctl має вбудовані game-root/park-dir; ці поля керують лише таблицею нижче, тому вони мають збігатися.",
    "Enable / disable deployed mods (nexus_core.modctl park/unpark)": "Увімкнення / вимкнення розгорнутих модів (nexus_core.modctl park/unpark)",
    "Only parked mods are selected. Unpark them first — their files are in the park folder, so deleting from the game folder would do nothing.": "Вибрано лише припарковані моди. Спочатку поверніть їх — їхні файли знаходяться в папці паркування, тому видалення з папки гри нічого не зробить.",
    "filter by name…": "фільтр за назвою…",
    "Allow parking frameworks (--force)": "Дозволити паркування фреймворків (--force)",
    "Park selected": "Припаркувати вибрані",
    "Unpark selected": "Повернути вибрані",
    "Unpark all": "Повернути всі",
    "Park": "Паркувати",
    "Unpark": "Повернути",
    "Restore every parked mod?": "Повернути всі припарковані моди?",
    "Select one or more Active mods first.": "Спочатку виберіть один або кілька активних модів.",
    "Select one or more Parked mods first.": "Спочатку виберіть один або кілька припаркованих модів.",
    "Select one or more mods to delete.": "Виберіть один або кілька модів для видалення.",
    "Profile library — DB state + hardlink rebuild": "Бібліотека профілів — стан БД + перебудова hardlink-посилань",
    "Save current as…": "Зберегти поточний як…",
    "Load + rebuild": "Завантажити + перебудувати",
    "Duplicate…": "Дублювати…",
    "Export…": "Експортувати…",
    "Import…": "Імпортувати…",
    "Profiles not loaded yet.": "Профілі ще не завантажено.",
    "Total mods": "Усього модів",
    "Saved profiles": "Збережені профілі",
    "Compare": "Порівняти",
    "Choose two profiles to compare enabled mods, conflicts, and load-order preferences.": "Виберіть два профілі, щоб порівняти ввімкнені моди, конфлікти та налаштування порядку завантаження.",
    "Compare profiles": "Порівняти профілі",
    "Profile Manager": "Менеджер профілів",
    "Load profile": "Завантажити профіль",
    "Overwrite profile": "Перезаписати профіль",
    "Profile exported": "Профіль експортовано",
    "Profile imported": "Профіль імпортовано",
    "Overwrite imported profile": "Перезаписати імпортований профіль",
    "Import profile preview": "Попередній перегляд імпорту профілю",
    # conflicts
    "Optional — enrich severity with live Nexus requirements": "Необов’язково — уточнювати серйозність за актуальними вимогами Nexus",
    "Scans canonical files in downloaded_mods: exact file overlaps, RED4 archive resource hashes, redscript annotations, and TweakXL keys. Nexus requirements are optional and only refine severity.": "Сканує основні файли в downloaded_mods: точні накладання файлів, хеші ресурсів архівів RED4, анотації redscript і ключі TweakXL. Вимоги Nexus необов’язкові й лише уточнюють серйозність.",
    "Multi-layer conflict engine": "Багаторівневий рушій конфліктів",
    "Scan conflicts": "Сканувати конфлікти",
    "Refresh inbox": "Оновити вхідні",
    "Disable mod…": "Вимкнути мод…",
    "Uninstall mod…": "Видалити мод…",
    "Set winner / load order…": "Задати переможця / порядок…",
    "Open only": "Лише відкриті",
    "No conflict scan loaded.": "Сканування конфліктів ще не завантажено.",
    "Conflict detail · every staged archive links to Nexus": "Деталі конфлікту · кожен staged-архів має посилання на Nexus",
    "Scanning staged files for conflicts…": "Сканування staged-файлів на конфлікти…",
    "Conflict detection": "Виявлення конфліктів",
    "Conflict preference": "Налаштування конфлікту",
    "Load-order/owner preference is only a safe resolver for file overlaps and archive-resource conflicts. Redscript and TweakXL collisions require disabling/uninstalling a mod or editing the mods themselves.": "Пріоритет порядку завантаження/власника безпечно вирішує лише накладання файлів і конфлікти ресурсів архівів. Конфлікти redscript і TweakXL потребують вимкнення/видалення мода або редагування самих модів.",
    "Uninstall staged mod": "Видалити staged-мод",
    "Disable mod": "Вимкнути мод",
    # installation
    "Re-evaluate dependent steps": "Перерахувати залежні кроки",
    "Installation paths": "Шляхи встановлення",
    "Refresh archives": "Оновити архіви",
    "Preview selected": "Переглянути вибране",
    "FOMOD wizard…": "Майстер FOMOD…",
    "Skip all conflicts": "Пропустити всі конфлікти",
    "Overwrite conflicts": "Перезаписати конфлікти",
    "Install resolved plan": "Встановити узгоджений план",
    "Downloaded archives": "Завантажені архіви",
    "Select an archive and preview it before installation.": "Виберіть архів і перегляньте його перед встановленням.",
    "Pre-install preview / conflicts": "Попередній перегляд / конфлікти",
    "Managed mod": "Керований мод",
    "Refresh installed": "Оновити встановлені",
    "Enable / Disable selected": "Увімкнути / вимкнути вибране",
    "Enable / disable managed mods": "Увімкнення / вимкнення керованих модів",
    "Rebuilding preview with FOMOD selections…": "Перебудова попереднього перегляду з вибором FOMOD…",
    "Installer steps": "Кроки інсталятора",
    "No optional choices": "Немає необов’язкових варіантів",
    "This FOMOD only contains required or conditional files.": "Цей FOMOD містить лише обов’язкові або умовні файли.",
    "FOMOD dependencies changed": "Залежності FOMOD змінилися",
    "Your choices revealed another installer step. Review the newly visible options, then press OK again.": "Ваш вибір відкрив ще один крок інсталятора. Перегляньте нові варіанти й натисніть OK ще раз.",
    "Select a downloaded archive first.": "Спочатку виберіть завантажений архів.",
    "Mod Installation": "Встановлення модів",
    "Install mod": "Встановити мод",
    "Manage": "Керування",
    "Select a managed mod first.": "Спочатку виберіть керований мод.",
    # advanced deploy
    "optional: names / globs to limit (space separated)": "необов’язково: назви / glob-шаблони для обмеження (через пробіл)",
    "optional NexusMods API key (deployer hash/metadata)": "необов’язковий API-ключ NexusMods (хеші/метадані deployer)",
    "Force re-deploy installed mods": "Примусово розгорнути встановлені моди повторно",
    "Verbose (per-file conflicts)": "Докладно (конфлікти по файлах)",
    "Keep superseded older versions": "Зберігати замінені старі версії",
    "Skip hash verification": "Пропустити перевірку хешів",
    "Dry run": "Пробний запуск",
    "Deploy": "Розгорнути",
    "List deployed": "Показати розгорнуті",
    "Check updates": "Перевірити оновлення",
    "Health check": "Перевірка стану",
    "Extract & deploy into the game folder": "Розпакувати й розгорнути в папку гри",
    "Redscript compile failed": "Компіляція redscript не вдалася",
    "Updates available": "Доступні оновлення",
    "Case-duplicate folders found": "Знайдено папки-дублікати за регістром",
    "Done — no updates to download.": "Готово — немає оновлень для завантаження.",
    "Missing requirements": "Відсутні вимоги",
    "Updates downloaded": "Оновлення завантажено",
    "Some updates failed": "Деякі оновлення не вдалися",
    "Mod will crash the game": "Мод спричинить збій гри",
    "Broken cross-mod lookups": "Зламані міжмодові посилання",
    "Per-frame script cost is high": "Високе навантаження скриптів на кадр",
    "A pinned file changed": "Закріплений файл змінився",
    "Unfinished uninstall": "Незавершене видалення",
    "Conflicting mods": "Конфліктні моди",
    # load order
    "Dry-run preview": "Пробний перегляд",
    "Apply + generate modlist.txt": "Застосувати + створити modlist.txt",
    "Reset to ASCII": "Скинути до ASCII",
    "Stop managing modlist.txt": "Припинити керування modlist.txt",
    "Archive load order — first row has highest priority / wins conflicts": "Порядок завантаження архівів — перший рядок має найвищий пріоритет / перемагає в конфліктах",
    "Deployed filename": "Ім’я розгорнутого файла",
    "Working order": "Робочий порядок",
    "Optional reason, e.g. compatibility patch wins": "Необов’язкова причина, напр. патч сумісності має пріоритет",
    "Add before → after rule": "Додати правило перед → після",
    "Remove selected rule": "Видалити вибране правило",
    "Preference rules — durable per profile": "Правила пріоритету — окремо для кожного профілю",
    "Load order not loaded yet.": "Порядок завантаження ще не завантажено.",
    "Dry-run / modlist.txt": "Пробний запуск / modlist.txt",
    "Working order changed locally — use Dry-run preview, then Apply.": "Робочий порядок змінено локально — виконайте пробний перегляд, потім застосуйте.",
    "Load Order": "Порядок завантаження",
    "Reset archive order": "Скинути порядок архівів",
    "Choose two different mods.": "Виберіть два різні моди.",
    "Existing modlist.txt": "Наявний modlist.txt",
    """Clear manual archive order, legacy mod priorities, and preference rules, then return to ASCII order?

This does not uninstall or disable any mod.""": """Очистити ручний порядок архівів, старі пріоритети модів і правила пріоритету, а потім повернутися до порядку ASCII?

Це не видалить і не вимкне жодного мода.""",
    """Disable automatic modlist.txt generation and remove the manager-owned hardlink?

An externally replaced modlist.txt will never be deleted.""": """Вимкнути автоматичне створення modlist.txt і видалити hardlink, яким керує менеджер?

Зовнішньо замінений modlist.txt ніколи не буде видалено.""",
    """The existing modlist.txt is not managed by this app.

Back it up under downloaded_mods/external_backups and replace it?""": """Наявний modlist.txt не керується цією програмою.

Створити резервну копію в downloaded_mods/external_backups і замінити його?""",
    """ASCII reset changed the desired archive filenames, but the existing modlist.txt is not managed by this app.

Back it up and replace it with the reset order?""": """Скидання до ASCII змінило бажані імена архівів, але наявний modlist.txt не керується цією програмою.

Створити резервну копію й замінити його порядком після скидання?""",
    # dialogs/deployment
    "Mod file": "Файл мода",
    "Delete mods": "Видалити моди",
    "Optional files download only when “Include optional” is ticked. External/manual files can’t be fetched by the tool — grab them from their mod pages.": "Необов’язкові файли завантажуються лише коли ввімкнено «Включати необов’язкові файли». Зовнішні/ручні файли інструмент не може отримати — завантажте їх зі сторінок відповідних модів.",
    "⚠ <b>Framework mod(s) selected.</b> Other mods depend on these; deleting them can break your load order.": "⚠ <b>Вибрано мод(и)-фреймворки.</b> Від них залежать інші моди; видалення може порушити порядок завантаження.",
    "Files also owned by a mod you keep are left in place. Empty folders are pruned afterwards.": "Файли, якими також володіє мод, що залишається, не видаляються. Порожні папки після цього очищаються.",
    "Permanently delete (skip the recoverable .mod_trash copy)": "Видалити назавжди (без відновлюваної копії .mod_trash)",
    "Dry run — preview only, delete nothing": "Пробний запуск — лише перегляд, нічого не видаляти",
    "I understand — delete the framework(s) anyway": "Я розумію — все одно видалити фреймворк(и)",
    "Clean game folder": "Чиста папка гри",
    "Deploy links": "Розгорнути посилання",
    "Undeploy": "Прибрати розгортання",
    "Check drift": "Перевірити відхилення",
    "Migrate legacy install…": "Мігрувати старе встановлення…",
    "Auto-deploy active profile when manager launches": "Автоматично розгортати активний профіль під час запуску менеджера",
    "New profile…": "Новий профіль…",
    "Switch + rebuild": "Перемкнути + перебудувати",
    "Profiles — DB flag + hardlink rebuild": "Профілі — прапорець БД + перебудова hardlink-посилань",
    "No deployment status loaded yet.": "Стан розгортання ще не завантажено.",
    "REDmod blocked": "REDmod заблоковано",
    "Active projection": "Активна проєкція",
    "Game path": "Шлях гри",
    "Drift detection": "Виявлення відхилень",
    "Start local API": "Запустити локальний API",
    "Stop API": "Зупинити API",
    "POST /deploy endpoint is stopped": "Ендпоінт POST /deploy зупинено",
    "Local deployment endpoint (127.0.0.1 only)": "Локальний ендпоінт розгортання (лише 127.0.0.1)",
    "Smart deployment": "Розумне розгортання",
    "Undeploy active profile": "Прибрати розгортання активного профілю",
    """Remove all game-tree hardlinks managed by this app?

Staged mod content is kept in downloaded_mods and can be redeployed instantly.""": """Видалити всі hardlink-посилання в дереві гри, якими керує ця програма?

Staged-вміст модів залишиться в downloaded_mods і його можна миттєво розгорнути знову.""",
    """Convert the existing .mod_manifest.json in-place installation to managed staging?

Files are copied into downloaded_mods first, then the old files are replaced with hardlinks. Legacy manager JSON is moved into downloaded_mods/legacy_state/.""": """Перетворити наявне in-place встановлення .mod_manifest.json на керований staging?

Спочатку файли копіюються в downloaded_mods, потім старі файли замінюються hardlink-посиланнями. Старі JSON-файли менеджера переміщуються в downloaded_mods/legacy_state/.""",
    "Migrate legacy install": "Мігрувати старе встановлення",
    "Local API": "Локальний API",
    "Deployment": "Розгортання",
    # updates/detail
    "Check enabled mods only": "Перевіряти лише ввімкнені моди",
    "Mod Update Checker": "Перевірка оновлень модів",
    "Updates are matched against the exact installed Nexus file when possible (file ID → MD5 → version), then compared through that file's Nexus update lineage. Download uses the existing persistent Download Manager.": "Коли можливо, оновлення зіставляються з точним установленим файлом Nexus (file ID → MD5 → версія), а потім порівнюються за лінією оновлень цього файла. Завантаження використовує наявний постійний Download Manager.",
    "This is a free Nexus account. The selected update's Files page has been opened. Choose Download with Manager there; the registered NXM handler will send the archive to Downloads.": "Це безкоштовний акаунт Nexus. Сторінку Files вибраного оновлення відкрито. Виберіть там Download with Manager; зареєстрований NXM-обробник передасть архів у Завантаження.",
    "Check for updates": "Перевірити оновлення",
    "Download update": "Завантажити оновлення",
    "Updates have not been checked yet.": "Оновлення ще не перевірялися.",
    "Matched by": "Зіставлено за",
    "Comparing staged mods with Nexus file metadata…": "Порівняння staged-модів із метаданими файлів Nexus…",
    "Update operation failed; see log.": "Операція оновлення не вдалася; див. журнал.",
    "Nexus authorization required": "Потрібна авторизація Nexus",
    "Waiting for Nexus Download-with-Manager authorization.": "Очікування авторизації Nexus Download-with-Manager.",
    "Update downloaded": "Оновлення завантажено",
    "Update downloaded to Archive Management.": "Оновлення завантажено до Archive Management.",
    "Install selected file": "Встановити вибраний файл",
    # correlation
    "Nexus Mods API key (network matching tiers)": "API-ключ Nexus Mods (мережеві рівні зіставлення)",
    "One scan root per line; relative paths resolve under the game root.": "Один корінь сканування в рядку; відносні шляхи визначаються від кореня гри.",
    "Collection slug or URL; comma-separate multiple collections": "Slug або URL колекції; кілька колекцій розділяйте комами",
    "Nexus Auto-Correlation": "Автозіставлення Nexus",
    "Evidence order: filename IDs → MD5 → file-content reverse lookup → endorsed/tracked → collections → requirements → Jaccard + Jaro-Winkler. Suggestions do not change deployment ownership; Confirm, Reject and Reassign are stored as review decisions.": "Порядок доказів: ID з імен файлів → MD5 → зворотний пошук за вмістом → схвалені/відстежувані → колекції → вимоги → Jaccard + Jaro-Winkler. Підказки не змінюють власника розгортання; Confirm, Reject і Reassign зберігаються як рішення перевірки.",
    "A Nexus API key is required to search by mod name. You can still enter a numeric mod ID or Nexus URL.": "Для пошуку за назвою мода потрібен API-ключ Nexus. Ви все ще можете ввести числовий ID мода або URL Nexus.",
    "Run Auto-Correlation": "Запустити автозіставлення",
    "Reload saved review": "Перезавантажити збережену перевірку",
    "No correlation run loaded.": "Результати зіставлення ще не завантажено.",
    "Local group": "Локальна група",
    "Nexus match": "Збіг Nexus",
    "Scanning local mods…": "Сканування локальних модів…",
    "Confirm current match": "Підтвердити поточний збіг",
    "Reject current match": "Відхилити поточний збіг",
    "Reassign to Nexus mod…": "Перепризначити мод Nexus…",
    "Loading saved correlation review…": "Завантаження збереженої перевірки зіставлення…",
    "Confirm": "Підтвердити",
    "Reject": "Відхилити",
    "Reassign…": "Перепризначити…",
    "No Nexus results": "Немає результатів Nexus",
    "No matching Nexus mods were returned.": "Відповідних модів Nexus не знайдено.",
    "No usable Nexus mod records were returned.": "Nexus не повернув придатних записів модів.",
    "Auto-Correlation": "Автозіставлення",
    "No saved correlation run yet.": "Збережених результатів зіставлення ще немає.",
    # archives
    "Downloaded archive staging": "Staging завантажених архівів",
    "Import archive…": "Імпортувати архів…",
    "Browse contents": "Переглянути вміст",
    "Link Nexus…": "Прив’язати Nexus…",
    "Unlink Nexus": "Відв’язати Nexus",
    "Delete archive…": "Видалити архів…",
    "Clean orphans…": "Очистити сироти…",
    "Archive library not loaded yet.": "Бібліотека архівів ще не завантажена.",
    "Installed by": "Встановлено через",
    "Managed package archives": "Керовані архіви пакетів",
    "Select an archive and choose Browse contents.": "Виберіть архів і натисніть «Переглянути вміст».",
    "Archive contents — metadata only, no extraction": "Вміст архіву — лише метадані, без розпакування",
    "Archives": "Архіви",
    "Unlink Nexus mod": "Відв’язати мод Nexus",
    "Delete archive": "Видалити архів",
    "Archive cleanup": "Очищення архівів",
    "No orphaned archives were found.": "Архівів-сиріт не знайдено.",
    "Clean orphaned archives": "Очистити архіви-сироти",
}

# Common generated statuses/dialogs.  Patterns are deliberately narrow so they do
# not touch Nexus-authored prose.
_PATTERNS: list[tuple[re.Pattern[str], Callable[[re.Match[str]], str]]] = [
    (re.compile(r"^Game root not found:\n?(.*)$", re.S), lambda m: "Кореневу папку гри не знайдено:" + ("\n" + m.group(1) if m.group(1) else "")),
    (re.compile(r"^Game root not found: (.*)$", re.S), lambda m: f"Кореневу папку гри не знайдено: {m.group(1)}"),
    (re.compile(r"^Managed staging: (.*)$", re.S), lambda m: f"Керований staging: {m.group(1)}"),
    (re.compile(r"^✓ Connected as (.+) · Premium account$"), lambda m: f"✓ Підключено як {m.group(1)} · Premium-акаунт"),
    (re.compile(r"^✓ Connected as (.+) · Free account$"), lambda m: f"✓ Підключено як {m.group(1)} · безкоштовний акаунт"),
    (re.compile(r"^Loaded (\d+) profiles?\.?$"), lambda m: f"Завантажено профілів: {m.group(1)}."),
    (re.compile(r"^Found (\d+) mods?\.?$"), lambda m: f"Знайдено модів: {m.group(1)}."),
    (re.compile(r"^Found (\d+) archives?\.?$"), lambda m: f"Знайдено архівів: {m.group(1)}."),
    (re.compile(r"^([0-9]+) updates? available\.?$", re.I), lambda m: f"Доступно оновлень: {m.group(1)}."),
]


def set_current_language(code: object) -> str:
    global _CURRENT_LANGUAGE
    _CURRENT_LANGUAGE = normalize_language(code)
    return _CURRENT_LANGUAGE


def current_language() -> str:
    return _CURRENT_LANGUAGE


def tr(text: object, language: object | None = None) -> str:
    """Translate application-owned text, falling back to its English source."""
    value = str(text if text is not None else "")
    lang = normalize_language(language if language is not None else _CURRENT_LANGUAGE)
    if lang != LANG_UK or not value:
        return value
    exact = UK.get(value)
    if exact is not None:
        return exact
    for rx, fn in _PATTERNS:
        m = rx.match(value)
        if m:
            return fn(m)
    return value


def reverse_tr(text: object) -> str:
    value = str(text if text is not None else "")
    # Exact reverse is enough for fixed chrome. Dynamic text retains its English
    # source in QObject properties while it is displayed.
    return _UK_TO_EN.get(value, value)


_UK_TO_EN = {v: k for k, v in UK.items() if v and v != k}


def catalogue_coverage(strings: list[str] | tuple[str, ...] | set[str]) -> tuple[int, int]:
    """Return (translated, total) for tests/tooling."""
    values = [str(s) for s in strings if str(s).strip()]
    translated = sum(1 for s in values if tr(s, LANG_UK) != s or s in UK)
    return translated, len(values)


# --------------------------- Qt integration ---------------------------------
# Kept optional so localization unit tests can run in headless/minimal builders.
try:  # pragma: no cover - exercised on real GUI systems
    from PySide6.QtCore import QEvent, QObject, QTimer, Signal, Qt
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import (
        QApplication, QAbstractButton, QGroupBox, QLabel, QLineEdit, QMainWindow,
        QMenu, QMessageBox, QStatusBar, QTabWidget, QTableWidget, QWizardPage, QWidget,
    )
    QT_AVAILABLE = True
except Exception:  # pragma: no cover
    QT_AVAILABLE = False


if QT_AVAILABLE:  # pragma: no cover - no PySide6 in the CI container
    class LanguageManager(QObject):
        languageChanged = Signal(str)

        def __init__(self, app: QApplication, settings):
            super().__init__(app)
            self.app = app
            self.settings = settings
            saved = settings.value("ui/language", "")
            self.language = normalize_language(saved or system_language())
            set_current_language(self.language)
            if not saved:
                settings.setValue("ui/language", self.language)
            self._translation_error_reported = False
            app.installEventFilter(self)
            # Do not periodically walk the complete application tree.  The GUI
            # has many tabs/tables, and the old 650 ms full-tree pass could keep
            # the Qt event loop busy long enough for the window manager to mark
            # the application as hung.  Normal operation is event-driven; a
            # complete pass is only needed when the user actually changes the
            # language.

        def set_language(self, code: object, *, persist: bool = True):
            lang = normalize_language(code)
            if persist:
                self.settings.setValue("ui/language", lang)
                self.settings.sync()
            if lang == self.language:
                self.retranslate_all()
                return
            self.language = lang
            set_current_language(lang)
            self.retranslate_all()
            self.languageChanged.emit(lang)

        def _report_translation_error(self, exc: BaseException):
            # A translator bug must never take down Qt's global event loop.  In
            # particular, exceptions escaping a Python eventFilter override are
            # re-reported by PySide for every subsequent event, producing a huge
            # recursive-looking error flood.  Report the first failure and keep
            # the rest of the application usable.
            if self._translation_error_reported:
                return
            self._translation_error_reported = True
            print(f"[i18n] translation error suppressed: {exc!r}", file=sys.stderr)

        def _safe_translate_object(self, obj, *, recursive: bool = True):
            try:
                self._translate_object(obj, recursive=recursive)
            except Exception as exc:
                self._report_translation_error(exc)

        def eventFilter(self, watched, event):
            try:
                etype = event.type()
                if etype == QEvent.Type.ChildAdded:
                    # ChildAdded is delivered to the parent.  Translating the
                    # whole parent subtree here is both unnecessary and very
                    # expensive for QTabWidget/QTableWidget hierarchies.
                    child = event.child()
                    if child is not None:
                        QTimer.singleShot(0, lambda o=child: self._safe_translate_object(o, recursive=False))
                elif etype in (QEvent.Type.Show, QEvent.Type.Polish):
                    # Translate only the object receiving this event.  Children
                    # receive their own ChildAdded/Polish/Show events.
                    QTimer.singleShot(0, lambda o=watched: self._safe_translate_object(o, recursive=False))
            except Exception as exc:
                self._report_translation_error(exc)
            return False

        def retranslate_all(self):
            # Full traversal is intentionally reserved for an explicit language
            # change.  It is no longer run on a periodic timer.
            for widget in self.app.topLevelWidgets():
                self._safe_translate_object(widget, recursive=True)

        def _translated_property(self, obj: QObject, prop: str, getter, setter):
            try:
                current = str(getter() or "")
            except Exception:
                return
            if not current:
                return
            source_key = f"_ncm_i18n_source_{prop}"
            last_key = f"_ncm_i18n_last_{prop}"
            source = obj.property(source_key)
            last = obj.property(last_key)
            if source is None or (last is not None and current != str(last)):
                source = current
                obj.setProperty(source_key, source)
            translated = tr(source, self.language)
            if current != translated:
                try:
                    setter(translated)
                except Exception:
                    return
            obj.setProperty(last_key, translated)

        def _translate_item(self, item):
            if item is None:
                return
            source_role = int(Qt.ItemDataRole.UserRole) + 177
            last_role = source_role + 1
            try:
                current = str(item.text() or "")
                source = item.data(source_role)
                last = item.data(last_role)
                if source is None or (last is not None and current != str(last)):
                    source = current
                    item.setData(source_role, source)
                translated = tr(source, self.language)
                if current != translated:
                    item.setText(translated)
                item.setData(last_role, translated)
            except Exception:
                pass

        def _translate_action(self, action: QAction):
            self._translated_property(action, "text", action.text, action.setText)
            self._translated_property(action, "tooltip", action.toolTip, action.setToolTip)
            self._translated_property(action, "status", action.statusTip, action.setStatusTip)

        def _translate_object(self, obj, *, recursive: bool = True):
            if obj is None:
                return
            if isinstance(obj, QAction):
                self._translate_action(obj)
            # QWidget-owned properties.
            if isinstance(obj, QWidget):
                self._translated_property(obj, "window_title", obj.windowTitle, obj.setWindowTitle)
                self._translated_property(obj, "tooltip", obj.toolTip, obj.setToolTip)
            if isinstance(obj, QAbstractButton):
                self._translated_property(obj, "text", obj.text, obj.setText)
            if isinstance(obj, QLabel):
                self._translated_property(obj, "text", obj.text, obj.setText)
            if isinstance(obj, QGroupBox):
                self._translated_property(obj, "title", obj.title, obj.setTitle)
            if isinstance(obj, QLineEdit):
                self._translated_property(obj, "placeholder", obj.placeholderText, obj.setPlaceholderText)
            if isinstance(obj, QMenu):
                self._translated_property(obj, "menu_title", obj.title, obj.setTitle)
            if isinstance(obj, QStatusBar):
                self._translated_property(obj, "status_message", obj.currentMessage, obj.showMessage)
            if isinstance(obj, QWizardPage):
                self._translated_property(obj, "wizard_title", obj.title, obj.setTitle)
                self._translated_property(obj, "wizard_subtitle", obj.subTitle, obj.setSubTitle)
            if isinstance(obj, QMessageBox):
                self._translated_property(obj, "message_text", obj.text, obj.setText)
                self._translated_property(obj, "message_info", obj.informativeText, obj.setInformativeText)
                self._translated_property(obj, "message_detail", obj.detailedText, obj.setDetailedText)
            if isinstance(obj, QTabWidget):
                for i in range(obj.count()):
                    page = obj.widget(i)
                    current = obj.tabText(i)
                    source = page.property("_ncm_i18n_tab_source") if page else None
                    last = page.property("_ncm_i18n_tab_last") if page else None
                    if source is None or (last is not None and current != str(last)):
                        source = current
                        if page:
                            page.setProperty("_ncm_i18n_tab_source", source)
                    translated = tr(source or current, self.language)
                    if current != translated:
                        obj.setTabText(i, translated)
                    if page:
                        page.setProperty("_ncm_i18n_tab_last", translated)
            if isinstance(obj, QTableWidget):
                for col in range(obj.columnCount()):
                    self._translate_item(obj.horizontalHeaderItem(col))
                for row in range(obj.rowCount()):
                    self._translate_item(obj.verticalHeaderItem(row))
                    for col in range(obj.columnCount()):
                        # Only exact-known body values translate; mod/file/user data stays untouched.
                        item = obj.item(row, col)
                        if item and (item.text() in UK or item.text() in _UK_TO_EN):
                            self._translate_item(item)
            # Recursive traversal is used only for explicit language switches.
            # Event-filter translation above is deliberately shallow.
            if recursive and isinstance(obj, QObject):
                try:
                    children = obj.children()
                except Exception:
                    children = []
                for child in children:
                    if isinstance(child, QAction):
                        self._translate_action(child)
                    elif isinstance(child, QWidget):
                        self._translate_object(child, recursive=True)


def install_language_manager(app, settings):
    global _MANAGER
    if not QT_AVAILABLE:
        set_current_language(settings.value("ui/language", system_language()) if settings else system_language())
        return None
    if _MANAGER is None:
        _MANAGER = LanguageManager(app, settings)
    return _MANAGER


def language_manager():
    return _MANAGER


def set_language(code: object, *, persist: bool = True) -> str:
    lang = normalize_language(code)
    if _MANAGER is not None:
        _MANAGER.set_language(lang, persist=persist)
    else:
        set_current_language(lang)
    return lang
