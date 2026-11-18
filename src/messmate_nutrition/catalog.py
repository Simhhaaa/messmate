"""Curated records. Dish profiles are recipe estimates, not lab measurements."""

from __future__ import annotations

from decimal import Decimal
import re

from .models import FoodCategory, FoodRecord, MacroNutrients, RecipeAssumption, ServingEstimate, SourceReference

_FDC = SourceReference(
    name="USDA FoodData Central (Foundation Foods/SR Legacy reference composition)",
    url="https://fdc.nal.usda.gov/",
    note="Reference data source; only Banana is linked to an exact FDC record in this catalog. Other whole-food ingredient matches need item-level validation before production use.",
)
_RECIPE = SourceReference(
    name="MESSMATE hand-curated recipe assumption",
    url="https://fdc.nal.usda.gov/",
    note="Heuristic estimate informed by typical generic ingredient composition; not a direct USDA prepared-dish record or a verified mess recipe. Ingredient proportions and cooking fat need validation.",
)
_USDA_SR28_RICE = SourceReference(
    name="USDA National Nutrient Database for Standard Reference, Release 28",
    url="https://www.ars.usda.gov/ARSUserFiles/80400535/Data/SR/SR28/reports/sr28fg20.pdf",
    reference_id="NDB 20445",
    note="Rice, white, long-grain, regular, unenriched, cooked without salt; nutrients per 100 g; USDA common measure 1 cup = 185 g.",
)
_USDA_SR28_FRIES = SourceReference(
    name="USDA National Nutrient Database for Standard Reference, Release 28",
    url="https://www.ars.usda.gov/ARSUserFiles/80400535/Data/SR/SR28/reports/sr28fg21.pdf",
    reference_id="NDB 21138",
    note="Fast foods, potato, french fried in vegetable oil; nutrients per 100 g; USDA common medium serving = 117 g. Mess preparation/oil may differ.",
)


def _m(kcal: str, protein: str, carbs: str, fat: str) -> MacroNutrients:
    return MacroNutrients(*(Decimal(x) for x in (kcal, protein, carbs, fat)))


def _serving(label: str, typical: str, low: str, high: str, basis: str) -> ServingEstimate:
    return ServingEstimate(label, Decimal(typical), Decimal(low), Decimal(high), basis)


def _food(key: str, name: str, category: FoodCategory, macros: tuple[str, str, str, str] | None,
          serving: tuple[str, str, str, str, str] | None, confidence: str,
          aliases: tuple[str, ...] = (), recipe: str | None = None, components: tuple[str, ...] = (),
          unknown_reason: str = "", source_override: SourceReference | None = None,
          estimate_range: str | None = None) -> FoodRecord:
    known = macros is not None
    return FoodRecord(
        key=key, display_name=name, category=category,
        aliases=(name, *aliases),
        nutrition_per_100g=_m(*macros) if macros else None,
        serving=_serving(*serving) if serving else None,
        confidence=Decimal(confidence),
        source=(source_override or (SourceReference("USDA FoodData Central SR Legacy: Bananas, raw", "https://fdc.nal.usda.gov/food-details/173944/nutrients", "173944", "Per 100 g raw edible portion.") if key == "banana" else (_FDC if known and category in {FoodCategory.INGREDIENT, FoodCategory.WHOLE_FOOD} else (_RECIPE if known else None)))),
        recipe=RecipeAssumption(recipe, components) if recipe else None,
        review_required=not known,
        notes=unknown_reason,
        estimate_range=Decimal(estimate_range) if estimate_range else None,
    )


_ESTIMATE = SourceReference(
    name="MESSMATE hostel-mess average estimate (ESTIMATED TIER)",
    url="https://www.ICMR-NIN.res.in/",
    note=(
        "Computed from IFCT 2017 ingredient values (docs/ifct_2017_ingredient_references.csv) "
        "using the stated raw-dal, vegetable and oil ratios. Ratios are assumed mess averages, "
        "not weighed kitchen data; the record is protected by a ±25% range check."
    ),
)

# All nutrient quantities are per 100 g edible finished food. Recipe profiles are
# rounded estimates; portion weights are independent estimates, not scraped facts.
_RECORDS = [
    _food("banana", "Banana", FoodCategory.WHOLE_FOOD, ("89", "1.09", "22.84", "0.33"), ("1 medium edible portion", "118", "90", "150", "USDA generic medium banana edible weight"), "0.88", ("banana, raw",)),
    _food("masala_oats", "Masala Oats", FoodCategory.COMPLETE_DISH, ("105", "3.5", "15.0", "3.2"), ("1 bowl", "200", "150", "250", "Prepared bowl; water-heavy texture varies"), "0.58", recipe="Oats cooked with water, vegetables and modest oil/spices; no milk assumed.", components=("rolled oats", "mixed vegetables", "water", "cooking oil", "spices")),
    _food("muesli", "Muesli", FoodCategory.COMPLETE_DISH, ("380", "10.0", "67.0", "7.0"), ("dry serving", "45", "30", "60", "Dry cereal portion; milk is a separate menu item"), "0.65", recipe="Generic oat/grain/raisin cereal blend; nut and sugar proportions vary.", components=("rolled oats", "dried fruit", "grains", "optional nuts")),
    _food("sada_paratha", "Sada Paratha", FoodCategory.COMPLETE_DISH, ("290", "7.0", "45.0", "9.0"), ("1 medium piece", "70", "50", "100", "Cooked piece; size and frying fat vary"), "0.55", ("plain paratha",), "Whole-wheat dough with a modest amount of pan fat.", ("whole-wheat flour", "water", "cooking oil/ghee")),
    _food("chana_palak", "Chana Palak Ki Sabzi", FoodCategory.COMPLETE_DISH, ("125", "6.0", "16.0", "4.0"), ("1 katori", "150", "100", "220", "Cooked curry serving"), "0.52", recipe="Cooked chickpeas and spinach with onion/tomato gravy and modest oil.", components=("chickpeas", "spinach", "onion/tomato", "cooking oil", "spices")),
    _food("hot_milk", "Hot Milk (D)", FoodCategory.BEVERAGE, ("61", "3.2", "4.8", "3.3"), ("1 cup", "150", "100", "250", "Assumes plain whole cow's milk; cup volume varies"), "0.68", ("hot milk",), "Plain whole milk, heated; no sugar included.", ("whole milk",)),
    _food("cold_milk", "Cold Milk (D)", FoodCategory.BEVERAGE, ("61", "3.2", "4.8", "3.3"), ("1 cup", "150", "100", "250", "Assumes plain whole cow's milk; cup volume varies"), "0.68", ("cold milk",), "Plain whole milk; no sugar included.", ("whole milk",)),
    _food("tea_d", "Tea (D)", FoodCategory.BEVERAGE, None, ("1 cup", "150", "100", "250", "Assumed cup volume only; recipe is unknown"), "0.08", ("tea",), unknown_reason="Portal does not specify milk, sugar, brew strength or cup recipe; no macro value assigned."),
    _food("coffee_powder", "Coffee Powder", FoodCategory.INGREDIENT, None, None, "0.05", ("ground coffee", "instant coffee"), unknown_reason="Portal gives neither coffee type nor quantity; a dry ingredient has no defensible served portion here. No nutrition assigned."),
    _food("bread_butter_jam", "Bread/Butter/Jam", FoodCategory.FOOD_COMBINATION, ("310", "7.0", "49.0", "10.0"), ("2 bread slices with spreads", "100", "70", "140", "Combined assumed portion; bread/spread amounts vary"), "0.38", recipe="Two small bread slices, about 10 g butter and 15 g jam; composite is weight-weighted.", components=("white bread", "butter", "jam")),
    _food("arhar_dal", "Arhar Dal Home Style", FoodCategory.COMPLETE_DISH, ("105", "5.5", "15.0", "2.5"), ("1 katori", "180", "130", "250", "Prepared thin-to-medium dal"), "0.55", ("toor dal", "arhar dal"), "Cooked pigeon-pea dal diluted with water and a small tempering; recipe thickness/oil vary.", ("toor dal", "water", "oil", "spices")),
    _food("paneer_do_pyaza", "Paneer Do-Pyaza", FoodCategory.COMPLETE_DISH, ("190", "9.0", "8.0", "14.0"), ("1 katori", "180", "120", "250", "Cooked gravy serving"), "0.43", ("paneer do pyaza",), "Paneer and onion-forward gravy; paneer fraction, cream and oil are unknown.", ("paneer", "onion", "tomato/gravy", "cooking oil")),
    _food("ullipai_pacchadi", "Ullipai Pacchadi", FoodCategory.CONDIMENT, None, ("1 small portion", "25", "10", "40", "Weight only; identity and recipe unresolved"), "0.05", ("ullipaya pachadi", "onion pachadi"), unknown_reason="Name may refer to an onion chutney, but ingredients and preparation are not verified. Requires manual identification; no nutrition assigned."),
    _food("curd", "Curd", FoodCategory.COMPLETE_DISH, ("61", "3.5", "4.7", "3.3"), ("1 small bowl", "100", "75", "150", "Plain whole-milk curd assumed"), "0.62", ("plain yogurt", "dahi"), "Plain whole-milk cultured curd; fat content varies.", ("whole milk", "starter culture")),
    _food("green_salad", "Green Salad", FoodCategory.FOOD_COMBINATION, ("24", "1.2", "5.0", "0.2"), ("1 side portion", "100", "60", "150", "Cucumber/tomato/onion mix; dressing omitted"), "0.35", recipe="Unspecified raw salad approximated as cucumber, tomato and onion by weight; no dressing/salt counted.", components=("cucumber", "tomato", "onion")),
    _food("jeera_rice", "Jeera Rice", FoodCategory.COMPLETE_DISH, ("150", "3.0", "28.0", "2.5"), ("1 serving", "180", "120", "250", "Cooked rice serving; oil/ghee varies"), "0.62", ("cumin rice",), "Cooked white rice with cumin and a small amount of oil/ghee.", ("cooked white rice", "cumin", "oil/ghee")),
    _food("chapati", "Chapati", FoodCategory.COMPLETE_DISH, ("290", "9.0", "58.0", "3.5"), ("1 medium chapati", "40", "30", "60", "Cooked weight; size and added fat vary"), "0.64", ("roti", "plain roti"), "Whole-wheat flour flatbread; assumes no ghee brushed on.", ("whole-wheat flour", "water")),
    _food("veg_puff", "Veg Puff", FoodCategory.COMPLETE_DISH, ("310", "5.0", "34.0", "17.0"), ("1 piece", "80", "55", "110", "Baked puff pastry snack; size/filling vary"), "0.38", ("vegetable puff",), "Pastry shell and potato/vegetable filling; pastry fat content varies greatly.", ("puff pastry", "potato", "mixed vegetables")),
    _food("hot_coffee", "Hot Coffee", FoodCategory.BEVERAGE, None, ("1 cup", "150", "100", "250", "Assumed cup volume only; ingredients unknown"), "0.08", ("coffee",), unknown_reason="Milk, sugar, coffee concentration and cup size are unspecified; no macro value assigned."),
    _food("ketchup", "Ketchup", FoodCategory.CONDIMENT, ("112", "1.3", "26.0", "0.2"), ("1 sachet/small spoon", "15", "8", "25", "Small condiment portion"), "0.45", ("tomato ketchup",), "Generic sweetened tomato ketchup profile; brand varies.", ("tomato", "sugar", "vinegar")),
    _food("chole_masala", "Chole Masala", FoodCategory.COMPLETE_DISH, ("145", "7.0", "20.0", "4.0"), ("1 katori", "180", "130", "250", "Cooked chickpea curry; gravy/oil vary"), "0.55", ("chole", "chana masala"), "Cooked chickpeas in onion/tomato gravy with modest oil.", ("chickpeas", "onion/tomato", "cooking oil", "spices")),
    _food("jeera_aloo", "Jeera Aloo", FoodCategory.COMPLETE_DISH, ("135", "2.5", "21.0", "4.5"), ("1 katori", "150", "100", "220", "Cooked potato side; oil varies"), "0.48", recipe="Cooked potato tossed with cumin and modest oil.", components=("potato", "cumin", "cooking oil")),
    _food("rasam", "Rasam", FoodCategory.COMPLETE_DISH, ("35", "1.5", "5.0", "1.0"), ("1 bowl", "180", "120", "250", "Thin soup; lentil content varies"), "0.42", recipe="Thin tamarind/tomato broth with spices and a small tempering; diluted recipe assumed.", components=("tomato/tamarind", "spices", "small amount of lentil", "tempering")),
    _food("steamed_rice", "Steamed Rice", FoodCategory.COMPLETE_DISH,
          ("130", "2.69", "28.17", "0.28"),
          ("1 cooked cup", "185", "150", "220", "USDA SR28 common measure; cooked weight"),
          "0.52", ("plain steamed rice", "cooked white rice"),
          recipe="Generic plain long-grain white rice cooked in water without added salt or oil; local variety and water uptake may differ.",
          components=("white rice", "water"), source_override=_USDA_SR28_RICE),
    _food("french_fries", "French Fries", FoodCategory.COMPLETE_DISH,
          ("312", "3.43", "41.44", "14.73"),
          ("1 medium serving", "117", "71", "154", "USDA SR28 common measures; prepared weight"),
          "0.52", ("french-fried potato",),
          recipe="Generic potato strips fried in vegetable oil; oil uptake and cut vary by preparation.",
          components=("potato", "vegetable oil"), source_override=_USDA_SR28_FRIES),
    # ESTIMATED TIER: hostel-mess averages computed from IFCT 2017 ingredient values.
    # Confidence sits at the 0.50 eligibility floor and every plan that includes
    # one must pass the ±25% low/high range check. Ratios are assumptions to confirm.
    _food("mix_yellow_dal", "Mix Yellow Dal", FoodCategory.COMPLETE_DISH,
          ("108", "6.02", "13.14", "3.28"),
          ("1 katori", "180", "130", "250", "Cooked dal; estimated serving weight"),
          "0.50", ("mix yellow dal", "yellow dal"),
          recipe="ESTIMATE: per 100 g cooked = 15 g green gram dal (IFCT B010) + 10 g lentil dal (IFCT B013) + 3 g oil, raw-dal basis; toor dal not in references.",
          components=("green gram dal", "lentil dal", "oil"), source_override=_ESTIMATE, estimate_range="0.25"),
    _food("menthi_dal_tadka", "Menthi Dal Tadka", FoodCategory.COMPLETE_DISH,
          ("109", "6.04", "13.19", "3.35"),
          ("1 katori", "180", "130", "250", "Cooked dal; estimated serving weight"),
          "0.50", ("menthi dal", "methi dal tadka", "fenugreek dal"),
          recipe="ESTIMATE: per 100 g cooked = 25 g green gram dal (IFCT B010) + 2 g fresh fenugreek leaves (IFCT C020) + 3 g oil.",
          components=("green gram dal", "fenugreek leaves", "oil"), source_override=_ESTIMATE, estimate_range="0.25"),
    _food("palakura_pappu", "Palakura Pappu", FoodCategory.COMPLETE_DISH,
          ("94", "4.99", "10.72", "3.33"),
          ("1 katori", "180", "130", "250", "Cooked dal; estimated serving weight"),
          "0.50", ("palakura pappu", "spinach dal", "pesarapappu"),
          recipe="ESTIMATE: per 100 g cooked = 20 g green gram dal (IFCT B010) + 10 g spinach (IFCT C033) + 3 g oil.",
          components=("green gram dal", "spinach", "oil"), source_override=_ESTIMATE, estimate_range="0.25"),
    _food("panchratna_dal", "Panchratna Dal", FoodCategory.COMPLETE_DISH,
          ("108", "5.80", "12.68", "3.57"),
          ("1 katori", "180", "130", "250", "Cooked dal; estimated serving weight"),
          "0.50", ("panchratan dal", "five dal"),
          recipe="ESTIMATE: per 100 g cooked = 6.25 g each of bengal gram, black gram, green gram and lentil dal (IFCT B001/B003/B010/B013) + 3 g oil; toor not in references.",
          components=("bengal gram dal", "black gram dal", "green gram dal", "lentil dal", "oil"), source_override=_ESTIMATE, estimate_range="0.25"),
    _food("pappu_charu", "Pappu Charu", FoodCategory.COMPLETE_DISH,
          ("50", "2.39", "5.26", "2.13"),
          ("1 bowl", "180", "120", "250", "Thin dal broth; estimated serving weight"),
          "0.50", ("pappu charu", "dal rasam"),
          recipe="ESTIMATE: per 100 g cooked = 10 g green gram dal (IFCT B010) + 2 g oil, thinned broth with tamarind/tomato not counted.",
          components=("green gram dal", "oil", "tamarind/tomato broth"), source_override=_ESTIMATE, estimate_range="0.25"),
    _food("dhaba_dal", "Dhaba Dal", FoodCategory.COMPLETE_DISH,
          ("116", "5.93", "12.94", "4.30"),
          ("1 katori", "180", "130", "250", "Cooked dal; estimated serving weight"),
          "0.50", ("dhaba dal", "dhaba style dal"),
          recipe="ESTIMATE: per 100 g cooked = 12.5 g lentil dal (IFCT B013) + 12.5 g black gram dal (IFCT B003) + 4 g oil.",
          components=("lentil dal", "black gram dal", "oil"), source_override=_ESTIMATE, estimate_range="0.25"),
]

CATALOG: dict[str, FoodRecord] = {record.key: record for record in _RECORDS}


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()


_ALIAS_INDEX: dict[str, FoodRecord] = {}
for _record in _RECORDS:
    for _alias in _record.aliases:
        _normalized = normalize_name(_alias)
        if _normalized in _ALIAS_INDEX and _ALIAS_INDEX[_normalized] != _record:
            raise RuntimeError(f"duplicate nutrition alias: {_alias}")
        _ALIAS_INDEX[_normalized] = _record


def lookup_food(name: str) -> FoodRecord | None:
    """Exact normalized alias lookup only. Similarity never grants nutrition."""
    return _ALIAS_INDEX.get(normalize_name(name))
