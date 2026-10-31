
from __future__ import annotations

import argparse
import json
import re
import time
from contextlib import suppress
from datetime import date, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from core import import_events, init_db, validate_event

HEADERS = {"User-Agent": "OlimpNavigator/1.0 (+educational event discovery)"}
MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5,
          "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10,
          "ноябр": 11, "декабр": 12}
SUBJECT_MAP = {
    "программирован": "Информатика", "информат": "Информатика", "алгоритм": "Информатика",
    "анализ данных": "Информатика", "математ": "Математика",
    "робот": "Робототехника", "физик": "Физика", "хими": "Химия",
    "биолог": "Биология", "истори": "История", "обществ": "Обществознание",
    "литератур": "Литература", "англий": "Языки", "лингвист": "Языки",
    "искусств": "Искусство", "дизайн": "Искусство", "музык": "Искусство",
    "хореограф": "Искусство", "театр": "Искусство", "живопис": "Искусство",
    "предприним": "Предпринимательство", "проект": "Предпринимательство",
}


def fetch(session: requests.Session, url: str) -> BeautifulSoup:
  response = session.get(url, timeout=20)
  response.raise_for_status()
  return BeautifulSoup(response.text, "html.parser")


def clean(text: str) -> str:
      return re.sub(r"\s+", " ", text or "").strip()


def page_image(soup: BeautifulSoup, page_url: str) -> str | None:
    meta = (soup.find("meta", attrs={"property": "og:image"}) or
            soup.find("meta", attrs={"name": "twitter:image"}))
    candidate = urljoin(page_url, meta.get("content", "")) if meta else ""
    return candidate if urlparse(candidate).scheme in ("http", "https") and not candidate.endswith("/_blank") else None


def subjects_from(text: str) -> list[str]:
   lower = text.casefold()
   return sorted({subject for stem, subject in SUBJECT_MAP.items() if stem in lower})


def grades_from(text: str) -> list[int]:
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    match = re.search(r"(\d{1,2})\s*-\s*(\d{1,2})\s*класс", text, re.I)
    if match:
        low, high = map(int, match.groups())
        return list(range(low, min(high, 11) + 1)) if 1 <= low <= high <= 11 else []
    match = re.search(r"(\d{1,2})\s*(?:-?х?\s*)?класс", text, re.I)
    return [int(match.group(1))] if match and 1 <= int(match.group(1)) <= 11 else []


def base_event(event_id: str, title: str, kind: str, url: str, today: date) -> dict:
    return {
        "id": event_id, "title": title, "kind": kind, "subjects": [], "grades": [],
        "min_age": None, "max_age": None, "regions": ["Вся Россия"],
        "organizer": "См. источник", "format": "См. источник",
        "registration_deadline": None, "stages": [], "result_date": None,
        "benefits": "Проверьте положение организатора",
        "eligibility": "Проверьте положение организатора",
        "description": "Условия и детали участия - по ссылке на источник.",
        "preparation": [], "official_url": url, "source_url": url,
        "image_url": None,
        "checked_at": today.isoformat(),
    }


def collect_olimpiada(session: requests.Session, today: date) -> list[dict]:
    url = "https://olimpiada.ru/activities"
    soup = fetch(session, url)
    events = []
    for card in soup.select("#megalist .fav_olimp.olimpiada[act]"):
        act = card.get("act")
        title_node = card.select_one(".o-info .headline")
        if not act or not title_node:
            continue
        source = f"https://olimpiada.ru/activity/{act}"
        event = base_event(f"olimpiada-{act}", clean(title_node.get_text(" ", strip=True)),
                           "олимпиада", source, today)
        event["regions"] = []
        event["subjects"] = subjects_from(" ".join(x.get_text(" ", strip=True) for x in card.select(".subject_tag")))
        classes = card.select_one(".classes_dop")
        event["grades"] = grades_from(clean(classes.get_text(" ", strip=True))) if classes else []
        desc = card.select_one(".olimp_desc")
        if desc:
            event["description"] = clean(desc.get_text(" ", strip=True))[:900] or event["description"]
        event["eligibility"] = clean(classes.get_text(" ", strip=True)) if classes else "Проверьте положение организатора"
        future = []
        for marker in card.select(".tl_event[date]"):
            end = None
            with suppress(ValueError, OverflowError):
                start = date.fromisoformat(marker["date"])
                length = int(marker.get("length", "1"))
                end = start + timedelta(days=max(length, 1) - 1)
            if end is None:
                continue
            if end < today:
                continue
            title = marker.find("font")
            label = clean(title.get_text(" ", strip=True)) if title else clean(marker.get_text(" ", strip=True))
            if "регистрац" in label.casefold():
                if not event["registration_deadline"] or end.isoformat() < event["registration_deadline"]:
                    event["registration_deadline"] = end.isoformat()
            elif "результат" in label.casefold():
                event["result_date"] = start.isoformat()
            elif start >= today:
                future.append({"name": label[:90] or "Этап", "date": start.isoformat()})
        event["stages"] = future[:8]
        if not (event["registration_deadline"] or event["stages"] or event["result_date"]):
            continue
        with suppress(requests.RequestException):
            detail = fetch(session, source)
            event["image_url"] = page_image(detail, source)
            for link in detail.select(".contacts a[href]"):
                href = urljoin(source, link["href"])
                if urlparse(href).scheme in ("http", "https") and "olimpiada.ru" not in urlparse(href).netloc:
                    event["official_url"] = href
            for link in detail.select(".prep-steps a[href]")[:5]:
                href = urljoin(source, link["href"])
                if urlparse(href).scheme in ("http", "https"):
                    event["preparation"].append({"title": clean(link.get_text(" ", strip=True)), "url": href})
            features = clean(detail.select_one("#features").get_text(" ", strip=True)) if detail.select_one("#features") else ""
            if "заочная" in features.casefold() or "дистанционная" in features.casefold():
                event["format"] = "онлайн / дистанционно"
            elif "очная" in features.casefold():
                event["format"] = "очно"
        time.sleep(0.15)
        with suppress(ValueError):
            events.append(validate_event(event))
    return events


def year_from_page(text: str, today: date) -> tuple[int, int]:
    match = re.search(r"(20\d{2})\s*[/\u2013-]\s*(?:20)?(\d{2,4})", text)
    if match:
        start = int(match.group(1))
        end = int(match.group(2))
        return start, end if end > 100 else start // 100 * 100 + end
    return today.year, today.year + 1


def russian_day_month(text: str, start_year: int, end_year: int) -> str | None:
    text = re.sub(r"\b(\d{1,2})\s*[-\u2013]\s*\d{1,2}\s+(?=[а-я])", r"\1 ", text.casefold())
    match = re.search(r"\b(\d{1,2})\s+(январ\w*|феврал\w*|марта|март\w*|апрел\w*|мая|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)", text.casefold())
    if not match:
        return None
    word = match.group(2)
    month = next((n for stem, n in MONTHS.items() if word.startswith(stem)), None)
    if not month:
        return None
    year = start_year if month >= 8 else end_year
    with suppress(ValueError):
        return date(year, month, int(match.group(1))).isoformat()
    return None


def collect_urfu(session: requests.Session, today: date) -> list[dict]:
    url = "https://dovuz.urfu.ru/"
    soup = fetch(session, url)
    start_year, end_year = year_from_page(soup.get_text(" ", strip=True), today)
    events = []
    for article in soup.select("article"):
        heading = article.find("h3")
        link = next((a for a in article.find_all("a", href=True) if "регистрац" in a.get_text(" ", strip=True).casefold()), None)
        if not heading or not link:
            continue
        title = clean(heading.get_text(" ", strip=True))
        href = urljoin(url, link["href"])
        slug = re.sub(r"[^a-z0-9-]", "-", urlparse(href).path.casefold()).strip("-")[-38:]
        if not slug:
            continue
        text = clean(article.get_text(" ", strip=True))
        if "студенческая" in title.casefold() and "школь" not in text.casefold():
            continue
        event = base_event(f"urfu-{slug}", title, "олимпиада" if "олимпиад" in title.casefold() else "конкурс", href, today)
        event["subjects"] = subjects_from(title + " " + text)
        event["grades"] = grades_from(text)
        event["regions"] = []
        event["organizer"] = "УрФУ"
        event["description"] = clean(article.find("p").get_text(" ", strip=True))[:900] if article.find("p") else text[:900]
        image = article.find("img", src=True)
        event["image_url"] = urljoin(url, image["src"]) if image else None
        event["format"] = "очно и онлайн" if "онлайн и очно" in text.casefold() else "См. источник"
        event["benefits"] = "Уточняйте на сайте УрФУ" if "особые права" not in text.casefold() else "Особые права при поступлении - по положению"
        event["eligibility"] = f"Ученики {min(event['grades'])}-{max(event['grades'])} классов" if event["grades"] else "Проверьте положение организатора"
        reg_match = re.search(r"Регистрация до\s+\d{1,2}\s+\S+", text, re.I)
        if reg_match:
            event["registration_deadline"] = russian_day_month(reg_match.group(), start_year, end_year)
        if event["registration_deadline"] and event["registration_deadline"] < today.isoformat():
            continue
        events.append(validate_event(event))
    return events


def collect_hacklist(session: requests.Session, today: date) -> list[dict]:
    url = "https://hacklist.ru/"
    soup = fetch(session, url)
    events = []
    seen = set()
    for anchor in soup.select('h3 a[href^="/itevents/"]'):
        href = urljoin(url, anchor["href"])
        slug = urlparse(href).path.strip("/").split("/")[-1]
        if slug in seen:
            continue
        seen.add(slug)
        card = anchor.find_parent("div", style=re.compile("padding:14px"))
        if not card:
            continue
        text = clean(card.get_text(" ", strip=True))
        if "для школьников" not in text.casefold():
            continue
        when = card.find("time", datetime=True)
        start = when.get("datetime") if when else None
        if not start:
            continue
        if start and start < today.isoformat():
            continue
        title = clean(anchor.get_text(" ", strip=True))
        kind = "олимпиада" if "олимпиад" in title.casefold() else "хакатон"
        event = base_event(f"hacklist-{slug}"[:48], title, kind, href, today)
        with suppress(requests.RequestException):
            event["image_url"] = page_image(fetch(session, href), href)
        event["subjects"] = subjects_from(text) or ["Информатика"]
        event["grades"] = grades_from(text)
        event["organizer"] = "Уточняйте у организатора"
        event["format"] = "онлайн" if "онлайн" in text.casefold() else "очно"
        event["description"] = "Хакатон для школьников. Регистрация и точные условия - на странице события и у организатора."
        event["benefits"] = "Призы и условия уточняйте у организатора"
        event["stages"] = [{"name": "Хакатон", "date": start}] if start else []
        if "идея фикс" in title.casefold():
            event["official_url"] = "https://www.sferum.ru/hackathon_2026"
            event["organizer"] = "Сферум"
            event["min_age"] = 12
            event["max_age"] = 18
            event["registration_deadline"] = "2026-09-20"
            event["format"] = "онлайн, финал в Москве"
            event["eligibility"] = "Команда из педагога и 3-5 школьников или студентов; школьники 12-18 лет"
            event["description"] = ("Всероссийский хакатон «Идея Фикс»: команда создаёт бота или сервис "
                                    "для школьной жизни. Этап разработки - 20 октября - 8 ноября 2026, "
                                    "финал в Москве - 12 декабря 2026. Подробности на официальном сайте.")
            event["stages"] = [{"name": "Завершение разработки", "date": "2026-11-08"},
                               {"name": "Финал в Москве", "date": "2026-12-12"}]
        events.append(validate_event(event))
    return events


def collect_hse(session: requests.Session, today: date) -> list[dict]:
    url = "https://olymp.hse.ru/inter/"
    soup = fetch(session, url)
    text = clean(soup.get_text(" ", strip=True))
    if "Международная олимпиада молодежи" not in text:
        return []
    event = base_event("hse-international-youth", "Международная олимпиада молодежи", "олимпиада", url, today)
    event["image_url"] = page_image(soup, url)
    event["organizer"] = "НИУ ВШЭ"
    event["regions"] = []
    event["description"] = "Олимпиада НИУ ВШЭ для иностранных школьников. Направления и условия участия проверяйте на официальной странице."
    event["eligibility"] = "Иностранные школьники, подробные условия - на сайте НИУ ВШЭ"
    event["subjects"] = subjects_from(text[:3000])
    registration = re.search(r"Регистрация на МОМ[^:]*:\s*\d{1,2}\s+\S+\s*[\u2014\u2013-]\s*(\d{1,2}\s+\S+\s+20\d{2})", text, re.I)
    if registration:
        raw = registration.group(1)
        year = int(re.search(r"20\d{2}", raw).group())
        event["registration_deadline"] = russian_day_month(raw, year, year)
    dates = re.search(r"Даты проведения:\s*(\d{1,2}\s+\S+)\s*[\u2014\u2013-]\s*\d{1,2}\s+\S+\s+(20\d{2})", text, re.I)
    if dates:
        year = int(dates.group(2))
        start = russian_day_month(dates.group(1), year, year)
        if start:
            event["stages"] = [{"name": "Олимпиадные состязания", "date": start}]
    if event["registration_deadline"] and event["registration_deadline"] < today.isoformat() and not event["stages"]:
        return []
    return [validate_event(event)]


def collect_nto(session: requests.Session, today: date) -> list[dict]:
    url = "https://ntcontest.ru/"
    soup = fetch(session, url)
    text = clean(soup.get_text(" ", strip=True))
    if "Национальная технологическая олимпиада" not in text:
        return []
    event = base_event("nto-school", "Национальная технологическая олимпиада", "олимпиада", url, today)
    event["image_url"] = page_image(soup, url)
    event["organizer"] = "НТО"
    event["subjects"] = ["Информатика", "Робототехника", "Предпринимательство"]
    event["grades"] = list(range(5, 12))
    event["description"] = "Командные технологические соревнования по разным профилям. Выберите профиль и проверьте актуальные сроки на официальном сайте."
    event["eligibility"] = "Школьники 5-11 классов; условия зависят от трека"
    event["preparation"] = [{"title": "Задания прошлых лет", "url": "https://ntcontest.ru/tracks/"}]
    return [validate_event(event)]


def collect_prod(session: requests.Session, today: date) -> list[dict]:
    url = "https://prodcontest.com/hackathon/"
    soup = fetch(session, url)
    text = clean(soup.get_text(" ", strip=True))
    if "Открытые хакатоны" not in text or "Прошедшие хакатоны" not in text:
        return []
    section = text.split("Открытые хакатоны", 1)[1].split("Прошедшие хакатоны", 1)[0][:700]
    pattern = re.compile(r"(\d{1,2})\s*[\u2014\u2013-]\s*\d{1,2}\s+(октября|ноября|декабря|января|февраля|марта|апреля|мая|июня)\s+(.{3,120}?)\s+(Регистрация закрыта|Зарегистрироваться)", re.I)
    events = []
    for match in pattern.finditer(section):
        if "закрыта" in match.group(4).casefold():
            continue
        month = next((number for stem, number in MONTHS.items() if match.group(2).casefold().startswith(stem)), None)
        if not month:
            continue
        year = today.year if month >= today.month else today.year + 1
        start = None
        with suppress(ValueError):
            start = date(year, month, int(match.group(1)))
        if start is None:
            continue
        if start < today:
            continue
        location = clean(match.group(3).split("ул.")[0].split("Международный университет")[0])
        city = next((city for city in ("Новосибирск", "Казань", "Мурманск", "Астана", "Санкт-Петербург") if city in location), location[:35])
        slug = {"Новосибирск": "novosibirsk", "Казань": "kazan", "Мурманск": "murmansk", "Астана": "astana", "Санкт-Петербург": "spb"}.get(city, "other")
        event = base_event(f"prod-hackathon-{year}-{slug}", f"Хакатон PROD - {city}", "хакатон", url, today)
        event["image_url"] = page_image(soup, url)
        event["organizer"] = "PROD"
        event["subjects"] = ["Информатика"]
        event["grades"] = [8, 9, 10, 11]
        event["regions"] = []
        event["format"] = "очно"
        event["description"] = f"Командный хакатон по разработке IT-продукта в городе {city}. Регистрация и условия - на официальном сайте PROD."
        event["eligibility"] = "Школьники 8-11 классов"
        event["stages"] = [{"name": "Хакатон", "date": start.isoformat()}]
        event["benefits"] = "Дипломы и сертификаты; детали - на сайте PROD"
        events.append(validate_event(event))
    return events


def collect_sirius(session: requests.Session, today: date) -> list[dict]:
    events = []
    for area in ("nauka", "iskusctvo", "sport", "literature"):
        url = f"https://sochisirius.ru/obuchenie/{area}"
        soup = fetch(session, url)
        for box in soup.select(".education__box"):
            heading = box.find("h3")
            link = box.find("a", href=re.compile(r"/obuchenie/"))
            if not heading or not link:
                continue
            href = urljoin(url, link["href"])
            match = re.search(r"smena(\d+)", href)
            if not match:
                continue
            title = clean(heading.get_text(" ", strip=True))
            period = box.select_one(".education_period")
            period_text = clean(period.get_text(" ", strip=True)) if period else ""
            year_match = re.search(r"(20\d{2})", period_text)
            year = int(year_match.group(1)) if year_match else today.year
            start = russian_day_month(period_text, year, year)
            if start and start < today.isoformat():
                continue
            event = base_event(f"sirius-{match.group(1)}", title, "образовательная программа", href, today)
            image = box.find("img", src=True)
            event["image_url"] = urljoin(url, image["src"]) if image else None
            event["subjects"] = subjects_from(title) or ["Другое"]
            event["organizer"] = "Образовательный центр «Сириус»"
            event["format"] = "очно, уточняйте условия"
            event["description"] = f"Образовательная программа «Сириуса». Период: {period_text}. Условия отбора и сроки подачи заявки смотрите на странице программы."
            event["stages"] = [{"name": "Начало программы", "date": start}] if start else []
            events.append(validate_event(event))
    return events


def collect_msu(session: requests.Session, today: date) -> list[dict]:
    url = "https://olymp.msu.ru/rus/"
    soup = fetch(session, url)
    links = {}
    for anchor in soup.select('a[href*="/event/"]'):
        match = re.search(r"/event/(\d+)/", anchor.get("href", ""))
        if match:
            links[match.group(1)] = urljoin(url, anchor["href"])
    events = []
    for event_id, href in links.items():
        detail = None
        with suppress(requests.RequestException):
            detail = fetch(session, href)
        if detail is None:
            continue
        heading = detail.find("h1")
        if not heading:
            continue
        title = clean(heading.get_text(" ", strip=True))
        year = re.search(r"(20\d{2})/20\d{2}", title)
        if not year or int(year.group(1)) < today.year - 1:
            continue
        event = base_event(f"msu-{event_id}", f"Ломоносов: {title}", "олимпиада", href, today)
        event["organizer"] = "МГУ имени М. В. Ломоносова"
        event["regions"] = []
        event["subjects"] = subjects_from(title)
        event["image_url"] = page_image(detail, href)
        description = detail.select_one(".event__text")
        if description:
            event["description"] = clean(description.get_text(" ", strip=True))[:900]
            event["grades"] = grades_from(event["description"])
        event["eligibility"] = (f"Ученики {min(event['grades'])}-{max(event['grades'])} классов"
                                if event["grades"] else "Классы и условия проверьте на сайте МГУ")
        registration = next((clean(x.get_text(" ", strip=True)) for x in detail.select(".event__navlabel")
                             if "регистрация до" in x.get_text(" ", strip=True).casefold()), "")
        if registration:
            event["registration_deadline"] = russian_day_month(registration, int(year.group(1)), int(year.group(1)) + 1)
        date_node = detail.select_one(".event__date")
        if date_node:
            raw = clean(date_node.get_text(" ", strip=True))
            match = re.search(r"(\d{1,2})\s*[-\u2013]\s*\d{1,2}\s+([А-Яа-я]+)\s+(20\d{2})", raw)
            if match:
                start = russian_day_month(f"{match.group(1)} {match.group(2)}", int(match.group(3)), int(match.group(3)))
                if start and start >= today.isoformat():
                    event["stages"] = [{"name": "Отборочный этап", "date": start}]
        if event["registration_deadline"] and event["registration_deadline"] < today.isoformat() and not event["stages"]:
            continue
        events.append(validate_event(event))
        time.sleep(0.08)
    return events


def collect_herzen(session: requests.Session, today: date) -> list[dict]:
    url = "https://olymp.herzen.spb.ru/course/index.php?categoryid=16"
    soup = fetch(session, url)
    events = []
    for card in soup.select(".coursebox"):
        anchor = card.select_one('h3.coursename a[href*="/course/view.php"]')
        if not anchor:
            continue
        href = urljoin(url, anchor["href"])
        match = re.search(r"[?&]id=(\d+)", href)
        if not match:
            continue
        title = clean(anchor.get_text(" ", strip=True))
        summary = clean(card.get_text(" ", strip=True))
        if "олимпиад" not in title.casefold() or ("2027" not in summary and "2026" not in summary):
            continue
        event = base_event(f"herzen-{match.group(1)}", title, "олимпиада", href, today)
        event["organizer"] = "РГПУ имени А. И. Герцена"
        event["regions"] = []
        event["subjects"] = subjects_from(title)
        event["grades"] = grades_from(summary)
        event["description"] = summary[:900]
        event["eligibility"] = "Школьники; классы и условия проверьте на сайте организатора"
        if "2027" in summary and "март" in summary.casefold():
            event["stages"] = [{"name": "Заключительный этап (месяц уточняется)", "date": None}]
        events.append(validate_event(event))
    return events


COLLECTORS = (collect_olimpiada, collect_urfu, collect_sirius, collect_hacklist,
              collect_hse, collect_nto, collect_prod, collect_msu, collect_herzen)


def collect_all(today: date | None = None) -> list[dict]:
    today = today or date.today()
    session = requests.Session()
    session.headers.update(HEADERS)
    events = []
    for collector in COLLECTORS:
        with suppress(requests.RequestException, ValueError, KeyError, AttributeError):
            found = collector(session, today)
            events.extend(found)
        time.sleep(0.5)
    levels_complete = False
    with suppress(requests.RequestException, ValueError):
        levels = fetch_levels(session)
        for event in events:
            event["rsosh_level"] = (level_for(event["title"], levels)
                                    if event["kind"] == "олимпиада" else 0)
        levels_complete = True
    if not levels_complete:
        for event in events:
            event.pop("rsosh_level", None)
    return events


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="проверить сбор без записи в базу")
    parser.add_argument("--json", help="сохранить собранный каталог в JSON")
    args = parser.parse_args()
    events = collect_all()
    if args.json:
        with open(args.json, "w", encoding="utf-8") as file:
            json.dump(events, file, ensure_ascii=False, indent=2)
    if not args.dry_run:
        init_db()
        added, updated, changes = import_events(events)
        print(f"Собрано: {len(events)}. Добавлено: {added}. Обновлено: {updated}. Важных изменений: {len(changes)}.")
    else:
        print(f"Проверено событий: {len(events)}")


RSOSH_SOURCE_URL = "https://rsr-olymp.ru/"
RSOSH_YEAR = "2026/27"


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold().replace("ё", "е")).strip()


def fetch_levels(session: requests.Session) -> dict[tuple[str, str], int]:
    response = session.get(RSOSH_SOURCE_URL, timeout=20)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    if RSOSH_YEAR not in soup.get_text(" ", strip=True):
        raise ValueError("На сайте РСОШ не найден перечень 2026/27")
    table = next((table for table in soup.find_all("table")
                  if table.find("tr") and "Уровень" in table.find("tr").get_text(" ", strip=True)), None)
    if table is None:
        raise ValueError("Не найдена таблица уровней РСОШ")
    levels = {}
    name = ""
    for row in table.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
        if len(cells) == 5:
            name, profile, level = cells[1], cells[2], cells[4]
        elif len(cells) == 3:
            profile, level = cells[0], cells[2]
        else:
            continue
        if level in ("1", "2", "3"):
            levels[(_norm(name), _norm(profile))] = int(level)
    if len(levels) < 100:
        raise ValueError("Таблица РСОШ неполная")
    return levels


def level_for(title: str, levels: dict[tuple[str, str], int]) -> int | None:
    title = _norm(title)
    if title.startswith("ломоносов:"):
        if "2026/2027" not in title:
            return None
        profile = title.split(":", 1)[1].split(" - 20", 1)[0].strip()
        if profile in ("английский язык", "немецкий язык", "французский язык"):
            profile = "иностранный язык"
        return levels.get((_norm("Олимпиада школьников «Ломоносов»"), profile))
    if title.startswith("герценовская олимпиада школьников"):
        profile = title.removeprefix("герценовская олимпиада школьников").strip()
        profile = profile.removeprefix("по профилю").removeprefix("по").strip(" «»")
        if profile in ("английскому языку", "немецкому языку", "французскому языку", "испанскому языку", "китайскому языку"):
            profile = "иностранный язык"
        elif profile == "географии":
            profile = "география"
        return levels.get((_norm("Герценовская олимпиада школьников"), profile))
    aliases = (
        ("южно-российская межрегиональная олимпиада", "архитектура и искусство", "рисунок, живопись, композиция, черчение"),
        ("интернет-олимпиада школьников по физике", "Интернет-олимпиада школьников по физике", "физика"),
        ("основы православной культуры", "Общероссийская олимпиада школьников «Основы православной культуры»", "основы православной культуры"),
        ("росатом» по математике", "Отраслевая физико-математическая олимпиада школьников «Росатом»", "математика"),
        ("санкт-петербургская олимпиада школьников по математике", "Санкт-Петербургская олимпиада школьников", "математика"),
    )
    for fragment, name, profile in aliases:
        if _norm(fragment) in title:
            if name == "архитектура и искусство":
                return next((level for (official, subject), level in levels.items()
                             if "южно-российская межрегиональная олимпиада" in official
                             and subject == _norm(profile)), None)
            return levels.get((_norm(name), _norm(profile)))
    return None


if __name__ == "__main__":
    main()
