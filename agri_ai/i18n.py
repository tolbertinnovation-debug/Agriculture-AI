"""Farmer-facing message catalogue (English, Swahili, French).

Templates are kept short: most farmers read them as SMS on feature phones, where
each 160-character segment costs the partner money.
"""

from __future__ import annotations

SUPPORTED_LANGUAGES = ("en", "sw", "fr")

MESSAGES: dict[str, dict[str, str]] = {
    "help": {
        "en": "{brand}: WEATHER | PRICE <crop> | SELL <crop> <kg> [price] | ASK <question> | PEST <crop> <symptoms> | YES/NO <code> | STOP",
        "sw": "{brand}: HALI | BEI <zao> | UZA <zao> <kg> [bei] | ULIZA <swali> | WADUDU <zao> <dalili> | NDIYO/HAPANA <namba> | ACHA",
        "fr": "{brand}: METEO | PRIX <culture> | VENDRE <culture> <kg> [prix] | QUESTION <texte> | RAVAGEUR <culture> <symptômes> | OUI/NON <code> | STOP",
    },
    "welcome": {
        "en": "Welcome to {brand}! You will get weather, price and pest alerts for your farm. Reply HELP for commands, STOP to opt out.",
        "sw": "Karibu {brand}! Utapokea taarifa za hali ya hewa, bei na wadudu. Jibu MSAADA kwa maagizo, ACHA kusitisha.",
        "fr": "Bienvenue sur {brand} ! Vous recevrez des alertes météo, prix et ravageurs. Répondez AIDE pour les commandes, STOP pour arrêter.",
    },
    "stopped": {
        "en": "You will no longer receive alerts from {brand}. Reply START to rejoin.",
        "sw": "Hutapokea tena taarifa kutoka {brand}. Jibu ANZA kujiunga tena.",
        "fr": "Vous ne recevrez plus d'alertes de {brand}. Répondez START pour revenir.",
    },
    "started": {
        "en": "Alerts from {brand} are back on.",
        "sw": "Taarifa kutoka {brand} zimewashwa tena.",
        "fr": "Les alertes de {brand} sont réactivées.",
    },
    "unknown": {
        "en": "Sorry, we did not understand. Reply HELP for commands.",
        "sw": "Samahani, hatukuelewa. Jibu MSAADA kupata maagizo.",
        "fr": "Désolé, nous n'avons pas compris. Répondez AIDE pour les commandes.",
    },
    "unknown_crop": {
        "en": "Unknown crop '{crop}'. Try: {examples}.",
        "sw": "Zao '{crop}' halijulikani. Jaribu: {examples}.",
        "fr": "Culture '{crop}' inconnue. Essayez : {examples}.",
    },
    "need_location": {
        "en": "We need your farm location for this. Ask your field officer or set it in the app.",
        "sw": "Tunahitaji eneo la shamba lako. Muulize afisa wako au weka kwenye programu.",
        "fr": "Nous avons besoin de la localisation de votre ferme. Demandez à votre agent ou réglez-la dans l'application.",
    },
    "weather_header": {
        "en": "Weather {place}:",
        "sw": "Hali ya hewa {place}:",
        "fr": "Météo {place} :",
    },
    "weather_day": {
        "en": "{day} {tmin}-{tmax}C rain {rain}mm",
        "sw": "{day} {tmin}-{tmax}C mvua {rain}mm",
        "fr": "{day} {tmin}-{tmax}C pluie {rain}mm",
    },
    "weather_unavailable": {
        "en": "Weather forecast is unavailable right now. Please try later.",
        "sw": "Utabiri wa hali ya hewa haupatikani sasa. Jaribu baadaye.",
        "fr": "Prévisions indisponibles pour le moment. Réessayez plus tard.",
    },
    "price_line": {
        "en": "{crop} {market}: {price} {currency}/kg ({trend})",
        "sw": "{crop} {market}: {price} {currency}/kg ({trend})",
        "fr": "{crop} {market} : {price} {currency}/kg ({trend})",
    },
    "no_price": {
        "en": "No recent {crop} prices near you.",
        "sw": "Hakuna bei za karibuni za {crop} karibu nawe.",
        "fr": "Aucun prix récent pour {crop} près de chez vous.",
    },
    "listing_created": {
        "en": "Listed {qty}kg {crop}{price}. We will SMS you when a buyer matches. Ref L{id}.",
        "sw": "Umeorodhesha {qty}kg {crop}{price}. Tutakutumia SMS mnunuzi akipatikana. Kumb L{id}.",
        "fr": "{qty}kg de {crop}{price} mis en vente. Nous vous préviendrons dès qu'un acheteur correspond. Réf L{id}.",
    },
    "match_offer": {
        "en": "Buyer {buyer} wants {qty}kg {crop} at {price} {currency}/kg ({distance}km away). Reply YES {code} to accept or NO {code}.",
        "sw": "Mnunuzi {buyer} anataka {qty}kg {crop} kwa {price} {currency}/kg (km {distance}). Jibu NDIYO {code} kukubali au HAPANA {code}.",
        "fr": "L'acheteur {buyer} veut {qty}kg de {crop} à {price} {currency}/kg (à {distance}km). Répondez OUI {code} pour accepter ou NON {code}.",
    },
    "match_accepted": {
        "en": "Deal {code} confirmed: {qty}kg {crop} at {price} {currency}/kg. {buyer} will contact you on {buyer_contact}.",
        "sw": "Mkataba {code} umethibitishwa: {qty}kg {crop} kwa {price} {currency}/kg. {buyer} atawasiliana nawe {buyer_contact}.",
        "fr": "Accord {code} confirmé : {qty}kg de {crop} à {price} {currency}/kg. {buyer} vous contactera au {buyer_contact}.",
    },
    "match_declined": {
        "en": "Offer {code} declined. Your produce stays listed.",
        "sw": "Ofa {code} imekataliwa. Mazao yako bado yameorodheshwa.",
        "fr": "Offre {code} refusée. Votre produit reste en vente.",
    },
    "match_not_found": {
        "en": "No open offer with code {code}.",
        "sw": "Hakuna ofa iliyo wazi yenye namba {code}.",
        "fr": "Aucune offre ouverte avec le code {code}.",
    },
    "otp": {
        "en": "Your {brand} login code is {code}. It expires in 10 minutes. Do not share it.",
        "sw": "Nambari yako ya kuingia {brand} ni {code}. Inaisha baada ya dakika 10. Usimpe mtu.",
        "fr": "Votre code de connexion {brand} est {code}. Il expire dans 10 minutes. Ne le partagez pas.",
    },
    "diagnosis": {
        "en": "Likely {label} ({confidence}% sure). Do: {actions}",
        "sw": "Huenda ni {label} (uhakika {confidence}%). Fanya: {actions}",
        "fr": "Probablement {label} ({confidence}% sûr). À faire : {actions}",
    },
    "diagnosis_review": {
        "en": "We could not identify the problem with confidence. An extension officer will review and reply. Ref D{id}.",
        "sw": "Hatukuweza kutambua tatizo kwa uhakika. Afisa ugani atakagua na kukujibu. Kumb D{id}.",
        "fr": "Nous n'avons pas pu identifier le problème avec certitude. Un conseiller va examiner et répondre. Réf D{id}.",
    },
    "diagnosis_reviewed": {
        "en": "Officer review of D{id}: {label}. {notes}",
        "sw": "Ukaguzi wa afisa D{id}: {label}. {notes}",
        "fr": "Avis du conseiller sur D{id} : {label}. {notes}",
    },
    "retake_photo": {
        "en": "Photo is unclear ({reason}). Please retake in daylight, close to the affected leaf, holding the phone steady.",
        "sw": "Picha haiko wazi ({reason}). Tafadhali piga tena mchana, karibu na jani lililoathirika, ukishikilia simu imara.",
        "fr": "Photo peu nette ({reason}). Reprenez-la à la lumière du jour, près de la feuille atteinte, sans bouger.",
    },
    "outbreak": {
        "en": "ALERT: {pest} reported on {count} farms within {radius}km of you. Check your {crop} now. {action}",
        "sw": "TAHADHARI: {pest} imeripotiwa mashamba {count} ndani ya km {radius} kutoka kwako. Kagua {crop} sasa. {action}",
        "fr": "ALERTE : {pest} signalé dans {count} fermes à moins de {radius}km. Inspectez votre {crop} maintenant. {action}",
    },
    "price_alert": {
        "en": "{crop} at {market} is now {price} {currency}/kg ({direction} your {threshold} target).",
        "sw": "{crop} soko la {market} sasa ni {price} {currency}/kg ({direction} lengo lako {threshold}).",
        "fr": "{crop} à {market} est maintenant à {price} {currency}/kg ({direction} votre objectif de {threshold}).",
    },
    "price_move": {
        "en": "{crop} price at {market} {move} {pct}% this week to {price} {currency}/kg.",
        "sw": "Bei ya {crop} soko la {market} {move} {pct}% wiki hii hadi {price} {currency}/kg.",
        "fr": "Le prix du {crop} à {market} {move} de {pct}% cette semaine : {price} {currency}/kg.",
    },
    "advice_unavailable": {
        "en": "We could not answer right now. Your question was sent to an extension officer.",
        "sw": "Hatukuweza kujibu sasa. Swali lako limetumwa kwa afisa ugani.",
        "fr": "Nous ne pouvons pas répondre maintenant. Votre question a été transmise à un conseiller.",
    },
    "subscribed": {
        "en": "OK. We will alert you when {crop} goes {direction} {threshold} {currency}/kg.",
        "sw": "Sawa. Tutakujulisha {crop} ikienda {direction} {threshold} {currency}/kg.",
        "fr": "OK. Nous vous alerterons quand {crop} passera {direction} {threshold} {currency}/kg.",
    },
    # Weather alert bodies. {advice} is crop/stage specific text.
    "wx_heavy_rain": {
        "en": "Heavy rain expected {day} ({rain}mm). {advice}",
        "sw": "Mvua kubwa inatarajiwa {day} ({rain}mm). {advice}",
        "fr": "Fortes pluies prévues {day} ({rain}mm). {advice}",
    },
    "wx_dry_spell": {
        "en": "Dry spell: little rain for the next {days} days. {advice}",
        "sw": "Kipindi kikavu: mvua kidogo kwa siku {days} zijazo. {advice}",
        "fr": "Période sèche : peu de pluie dans les {days} prochains jours. {advice}",
    },
    "wx_heat": {
        "en": "Heat stress {day}: up to {tmax}C. {advice}",
        "sw": "Joto kali {day}: hadi {tmax}C. {advice}",
        "fr": "Stress thermique {day} : jusqu'à {tmax}C. {advice}",
    },
    "wx_frost": {
        "en": "Frost risk {day}: down to {tmin}C. {advice}",
        "sw": "Hatari ya baridi kali {day}: hadi {tmin}C. {advice}",
        "fr": "Risque de gel {day} : jusqu'à {tmin}C. {advice}",
    },
    "wx_strong_wind": {
        "en": "Strong wind {day} ({wind}km/h). {advice}",
        "sw": "Upepo mkali {day} ({wind}km/h). {advice}",
        "fr": "Vent fort {day} ({wind}km/h). {advice}",
    },
    "wx_spray_window": {
        "en": "Good spraying window {day}: dry and calm. {advice}",
        "sw": "Wakati mzuri wa kunyunyizia {day}: kavu na shwari. {advice}",
        "fr": "Bonne fenêtre de traitement {day} : sec et calme. {advice}",
    },
    "wx_planting_rains": {
        "en": "Planting rains: {rain}mm expected over the next 3 days from {day}. Prepare seed and land.",
        "sw": "Mvua za kupanda: {rain}mm zinatarajiwa siku 3 kuanzia {day}. Andaa mbegu na shamba.",
        "fr": "Pluies de semis : {rain}mm attendus sur 3 jours à partir de {day}. Préparez semences et terrain.",
    },
    # Advice fragments appended to weather alerts.
    "adv_harvest_before_rain": {
        "en": "Harvest mature {crops} and keep it dry.",
        "sw": "Vuna {crops} iliyokomaa na uihifadhi mahali pakavu.",
        "fr": "Récoltez votre {crops} mûr et gardez-le au sec.",
    },
    "adv_drainage": {
        "en": "Clear drainage channels; delay fertiliser and spraying until after the rain.",
        "sw": "Safisha mitaro; subiri mvua ipite kabla ya kuweka mbolea au kunyunyizia.",
        "fr": "Dégagez les rigoles ; reportez engrais et traitements après la pluie.",
    },
    "adv_critical_water": {
        "en": "Your {crop} is at {stage}, its most water-sensitive stage: mulch and irrigate if you can.",
        "sw": "{crop} yako iko katika hatua inayohitaji maji zaidi: weka matandazo na umwagilie ukiweza.",
        "fr": "Votre {crop} est au stade le plus sensible à l'eau : paillez et irriguez si possible.",
    },
    "adv_conserve_moisture": {
        "en": "Conserve soil moisture: mulch and weed to reduce water loss.",
        "sw": "Hifadhi unyevu wa udongo: weka matandazo na palilia.",
        "fr": "Conservez l'humidité du sol : paillez et désherbez.",
    },
    "adv_heat": {
        "en": "Water early morning or evening and mulch.",
        "sw": "Mwagilia asubuhi mapema au jioni na weka matandazo.",
        "fr": "Arrosez tôt le matin ou le soir et paillez.",
    },
    "adv_heat_flowering": {
        "en": "Your {crop} is flowering - heat now cuts yield. Water early morning or evening and mulch.",
        "sw": "{crop} yako inatoa maua - joto sasa hupunguza mavuno. Mwagilia asubuhi au jioni na weka matandazo.",
        "fr": "Votre {crop} est en floraison - la chaleur réduit le rendement. Arrosez tôt ou le soir et paillez.",
    },
    "adv_frost": {
        "en": "Cover nursery beds and seedlings overnight; irrigate in the evening to hold soil heat.",
        "sw": "Funika vitalu na miche usiku; mwagilia jioni kuhifadhi joto la udongo.",
        "fr": "Couvrez pépinières et plants la nuit ; irriguez le soir pour garder la chaleur du sol.",
    },
    "adv_wind_tall": {
        "en": "Do not spray. Prop bananas and stake tall crops.",
        "sw": "Usinyunyizie. Weka miti ya kushikilia migomba na mazao marefu.",
        "fr": "Ne traitez pas. Étayez les bananiers et tuteurez les cultures hautes.",
    },
    "adv_wind": {
        "en": "Do not spray. Secure stores and shade nets.",
        "sw": "Usinyunyizie. Imarisha stoo na nyavu za kivuli.",
        "fr": "Ne traitez pas. Sécurisez magasins et filets d'ombrage.",
    },
    "adv_spray": {
        "en": "Spray early morning or late afternoon; wear protective clothing.",
        "sw": "Nyunyizia asubuhi mapema au jioni; vaa mavazi ya kujikinga.",
        "fr": "Traitez tôt le matin ou en fin d'après-midi ; portez une protection.",
    },
    "stage_tip": {
        "en": "{crop} ({stage}): {tasks}",
        "sw": "{crop} ({stage}): {tasks}",
        "fr": "{crop} ({stage}) : {tasks}",
    },
}

WORDS: dict[str, dict[str, str]] = {
    "up": {"en": "up", "sw": "imepanda", "fr": "a augmenté"},
    "down": {"en": "down", "sw": "imeshuka", "fr": "a baissé"},
    "stable": {"en": "stable", "sw": "imetulia", "fr": "stable"},
    "above": {"en": "above", "sw": "juu ya", "fr": "au-dessus de"},
    "below": {"en": "below", "sw": "chini ya", "fr": "en dessous de"},
    "today": {"en": "today", "sw": "leo", "fr": "aujourd'hui"},
    "tomorrow": {"en": "tomorrow", "sw": "kesho", "fr": "demain"},
}

DAY_NAMES = {
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "sw": ["Jtt", "Jnn", "Jtn", "Alh", "Ijm", "Jms", "Jpl"],
    "fr": ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"],
}


def lang_or_default(lang: str | None) -> str:
    return lang if lang in SUPPORTED_LANGUAGES else "en"


def t(key: str, lang: str | None = "en", **kwargs) -> str:
    lang = lang_or_default(lang)
    template = MESSAGES[key].get(lang) or MESSAGES[key]["en"]
    return template.format(**kwargs)


def word(key: str, lang: str | None = "en") -> str:
    return WORDS[key].get(lang_or_default(lang), WORDS[key]["en"])


def day_label(day_offset: int, weekday: int, lang: str | None = "en") -> str:
    if day_offset == 0:
        return word("today", lang)
    if day_offset == 1:
        return word("tomorrow", lang)
    return DAY_NAMES[lang_or_default(lang)][weekday]
