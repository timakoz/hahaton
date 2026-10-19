"""Колонки таксационного описания в формате «Пример материалов ТО.xlsx» (выход = этот же формат)."""
from __future__ import annotations

VID = "Лесоустроительный номер выдела"
REG_VID = "Регистрационный номер выдела"
REG_QUARTER = "Регистрационный номер квартала"
REGION = "Код региона"

# --- лист «Общие» ---
AREA = "Площадь выдела"
STOCK = "Общий запас лесных насаждений"
LAND_TYPE_CODE = "Код типа земель"
LAND_TYPE = "Наименование типа земель"
LAND_KIND_CODE = "Код вида лесных земель"
LAND_KIND = "Наименование вида лесных земель"
NONFOREST_KIND_CODE = "Код вида нелесных земель"
NONFOREST_KIND = "Наименование вида нелесных земель"
FOREST_TYPE_CODE = "Код типа леса"
FOREST_TYPE = "Наименование типа леса"
TLU = "Наименование типа лесорастительных условий"
STOCK_DEAD = "Общий запас сухостоя"
STOCK_SPARSE = "Общий запас естественных редин"
STOCK_SINGLE = "Общий запас единичных деревьев"
STOCK_UNUSABLE = "Общий запас неликвидной древесины"
PLANT_YEAR = "Год посадки (начала формирования)"
STATE = "Наименование состояния лесного насаждения"
ECON_CAT = "Хозяйственная категория"
DOM_CODE = "Код преобладающей породы"
DOM_NAME = "Преобладающая порода"
BONITET_CODE = "Код класса бонитета"
BONITET = "Класс бонитета"

GENERAL_COLS = [
    VID, "Дата лесоустройства", AREA, STOCK, LAND_TYPE_CODE, LAND_TYPE, LAND_KIND_CODE, LAND_KIND,
    NONFOREST_KIND_CODE, NONFOREST_KIND, FOREST_TYPE_CODE, FOREST_TYPE, TLU, STOCK_DEAD, STOCK_SPARSE,
    STOCK_SINGLE, STOCK_UNUSABLE, PLANT_YEAR, STATE, "Код целевой породы",
    "Целевая порода (справочник) НСИ groupsTreeSpecies", "Наименование целевой породы", ECON_CAT,
    DOM_CODE, DOM_NAME, BONITET_CODE, BONITET,
]
# Итоговые величины на выдел: при делении выдела масштабируются пропорционально площади.
GENERAL_TOTALS = [STOCK, STOCK_DEAD, STOCK_SPARSE, STOCK_SINGLE, STOCK_UNUSABLE]
GENERAL_NUMERIC = [AREA, STOCK, STOCK_DEAD, STOCK_SPARSE, STOCK_SINGLE, STOCK_UNUSABLE, PLANT_YEAR]

# --- лист «Ярус насаждения» ---
L_NUM = "Номер лесного яруса"
L_MAIN = "Является ли ярус основным"
L_HEIGHT = "Средняя высота яруса лесного насаждения в метрах"
L_HEIGHT2 = "Средняя высота яруса лесного насаждения в метрах.1"   # дубль колонки в исходных данных
L_FORMULA = "Состав лесного насаждения"
L_AGE = "Средний возраст"
L_DIAM = "Средний диаметр ствола"
L_STOCK_HA = "Средний запас древесины лесного насаждения на 1 гектар"
L_DENSITY = "Полнота яруса"
S_CODE = "Код древесной породы"
S_NAME = "Наименование древесной породы либо кустарниковой породы"
S_DOM = "Является ли преобладающей древесной породой"
S_SHARE = "Доля участия древесной породы в составе яруса лесного насаждения в долях единицы"
S_AGE = "Возраст древесной породы (лет)"
S_HEIGHT = "Высота древесной породы в метрах с округлением до 1 метра"
S_DIAM = "Средний диаметр ствола в сантиметрах с округлением до 1 сантиметра"
S_AGE_CLASS = "Наименование класса возраста"
S_AGE_GROUP_CODE = "Код группы возраста лесного насаждения"
S_AGE_GROUP = "Наименование группы возраста лесного насаждения"
S_STOCK_HA = "Запас древесины составляющих древесных пород на 1 гектар"

LAYER_COLS = [
    VID, L_NUM, L_MAIN, L_HEIGHT, L_HEIGHT2, L_FORMULA, L_AGE, L_DIAM, L_STOCK_HA, L_DENSITY, S_CODE,
    S_NAME, S_DOM, S_SHARE, S_AGE, S_HEIGHT, S_DIAM, S_AGE_CLASS, S_AGE_GROUP_CODE, S_AGE_GROUP, S_STOCK_HA,
]
# Заголовки при записи: дубль колонки высоты пишем так же, как в образце.
LAYER_HEADERS = [L_HEIGHT if c == L_HEIGHT2 else c for c in LAYER_COLS]
LAYER_NUMERIC = [L_NUM, L_HEIGHT, L_HEIGHT2, L_AGE, L_DIAM, L_STOCK_HA, L_DENSITY, S_SHARE, S_AGE, S_HEIGHT,
                 S_DIAM, S_STOCK_HA]

# --- лист «Подрост» ---
U_HEIGHT = "Средняя высота подроста в метрах"
U_AGE = "Средний возраст лет"
U_COUNT = "Количество подроста на гектар"
U_FORMULA = "Состав подроста (формула состава)"
U_EVAL_CODE = "Код оценки подроста"
U_EVAL = "Оценка подроста"
U_CODE = "Код породы подроста"
U_NAME = "Наименование породы подроста"
U_SHARE = "Доля участия породы подроста"
U_DOM = "Преобладающая древесная порода подроста"

UNDERGROWTH_COLS = [VID, U_HEIGHT, U_AGE, U_COUNT, U_FORMULA, U_EVAL_CODE, U_EVAL, U_CODE, U_NAME, U_SHARE, U_DOM]
UNDERGROWTH_NUMERIC = [U_HEIGHT, U_AGE, U_COUNT, U_SHARE]

SHEETS = {"general": "Общие", "layers": "Ярус насаждения", "undergrowth": "Подрост"}

# Справочные коды НСИ для новых записей (из образца)
CODE_FOREST_LANDS = "3200001"
CODE_NONFOREST_LANDS = "3200002"
