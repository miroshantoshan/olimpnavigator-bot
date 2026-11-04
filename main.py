from __future__ import annotations
import asyncio
import hashlib
import html
import hmac
import importlib
import importlib.util
import json
import logging
import os
import re
import sqlite3
import tempfile
from contextlib import AsyncExitStack, suppress
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import aiohttp
from aiohttp import web

env_file = Path(__file__).with_name('.env')
if env_file.is_file():
    for line in env_file.read_text(encoding='utf-8').splitlines():
        if '=' not in line or line.lstrip().startswith('#'):
            continue
        name, value = line.split('=', 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(name.strip(), value)

from collect import collect_all
from core import (INTERESTS, POINTS, REGIONS, achievements_for, add_achievement, ai_messages_today, all_events,
                  create_custom_event,
                  due_notifications, end_ai_session, ensure_user, get_ai_session, get_event, get_user, import_events,
                  init_db, is_tracked, leaderboard, mark_admin_delivered, mark_review_notified, mark_sent,
                  matches, pending_achievement, pending_review_notice, pending_review_notifications, pending_reviews_for,
                  partner_users, recommended, remember_name, reserve_partner_message, review_achievement,
                  save_ai_session, subscribers_for, track, tracked_ids, update_user, use_ai_message)
from services import MaxAPI, MaxAPIError, ai_answer, russian_ssl
local_token = importlib.import_module('max_token') if importlib.util.find_spec('max_token') else None
MAX_BOT_TOKEN = getattr(local_token, 'MAX_BOT_TOKEN', os.getenv('MAX_BOT_TOKEN', ''))
ADMIN_IDS = getattr(local_token, 'ADMIN_IDS', [int(x) for x in os.getenv('MAX_ADMIN_IDS', '').split(',') if x.strip().isdigit()])
SUPPORT_ID = getattr(local_token, 'SUPPORT_ID', int(os.getenv('MAX_SUPPORT_ID', '0') or 0))
SUPPORT_URL = getattr(local_token, 'SUPPORT_URL', os.getenv('MAX_SUPPORT_URL', '').strip())
TIMEZONE = getattr(local_token, 'TIMEZONE', os.getenv('OLIMPNAV_TIMEZONE', 'Europe/Moscow'))
from collect import RSOSH_SOURCE_URL

ROOT = Path(__file__).parent
BANNER_NAMES = {
    'welcome': 'glavnaya', 'events': 'meropriyatiya', 'search': 'poisk',
    'favorites': 'izbrannoe', 'deadlines': 'sroki', 'portfolio': 'portfolio',
    'assistant': 'assistent', 'profile': 'profil',
}
BANNERS = {}
for section, filename in BANNER_NAMES.items():
    BANNERS[section] = ROOT / 'assets' / (filename + '.png')
PAGE = 6
REGION_PAGE = 12
AI_DAILY_LIMIT = 50
KINDS = ('олимпиада', 'конкурс', 'хакатон', 'образовательная программа')
MENU = [('📚 Мероприятия', 'events'), ('🔎 Поиск', 'search'), ('⭐ Избранное', 'favorites'),
        ('📅 Сроки', 'deadlines'), ('🏅 Портфолио', 'portfolio'),
        ('🤖 Нейросеть', 'assistant'), ('👤 Профиль', 'profile')]
STATE: dict[int, dict] = {}
REVIEW_NOTICE_LOCK = asyncio.Lock()


def bezopasniy_tekst(value):
  return html.escape(str(value or ''))


def tekst_ot_ii(value):
    safe = bezopasniy_tekst(value)
    safe = re.sub(r'\*\*([^*\n]+)\*\*', r'<b>\1</b>', safe)
    safe = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<i>\1</i>', safe)
    safe = re.sub(r'`([^`\n]+)`', r'<code>\1</code>', safe)
    lines = []
    for line in safe.splitlines():
        if line.startswith('## '):
            line = '<b>' + line[3:] + '</b>'
        elif line.startswith('# '):
            line = '<b>' + line[2:] + '</b>'
        elif line.startswith('- '):
            line = '• ' + line[2:]
        lines.append(line)
    return '\n'.join(lines)


AI_COMMON_WORDS = {'расскажи', 'про', 'что', 'это', 'какие', 'какой', 'какая',
                   'когда', 'где', 'как', 'мне', 'можно', 'участвовать',
                   'участие', 'срок', 'сроки', 'дата', 'даты', 'приз', 'призы',
                   'хакатон', 'хакатоне', 'олимпиада', 'олимпиаде', 'конкурс',
                   'подробнее', 'есть', 'будет', 'для', 'него', 'ней',
                   'рсош', 'всош', 'уровень', 'уровня', 'уровни', 'этап', 'этапы'}


def slova_dlya_ii(text):
      words = re.findall(r'[a-zа-яё0-9]+', text.casefold().replace('ё', 'е'))
      return [word for word in words if len(word) > 2 and word not in AI_COMMON_WORDS]


def kartochki_dlya_ii(question, history, user, selected_id=None):
    if selected_id:
        card = get_event(selected_id)
        if card:
            return [card]
    events = all_events()
    level_question = bool(re.search(r'всош|рсош|уровн|этап', question.casefold()))
    terms = slova_dlya_ii(question)
    previous = [turn.get('text', '') for turn in reversed(history or [])
                if turn.get('role') == 'user']
    for text in [question] + ([] if level_question else previous):
        terms = slova_dlya_ii(text)
        if not terms:
            continue
        scores = []
        for event in events:
            title = event['title'].casefold().replace('ё', 'е')
            details = (event['organizer'] + ' ' + event['description']).casefold().replace('ё', 'е')
            if level_question and not all(word[:5] in title for word in terms):
                continue
            score = sum(6 if word in title else 1 if word in details else 0 for word in terms)
            if score:
                scores.append((score, event))
        if scores:
            scores.sort(key=lambda item: (-item[0], item[1]['title']))
            return [event for _, event in scores[:6]]
    if level_question:
        return []
    return recommended(user, segodnya())[:6] if profil_gotov(user) else events[:6]


def segodnya():
   return datetime.now(ZoneInfo(TIMEZONE)).date()


def knopka(label, value):
   return {'type': 'callback', 'text': label[:128], 'payload': value}


def ssylka(label, url):
     return {'type': 'link', 'text': label[:128], 'url': url}


def zadaniya_proshlyh_let(event):
    event_id = event['id']
    if event_id.startswith('olimpiada-'):
        for item in event.get('preparation', []):
            url = item.get('url', '')
            if 'olimpiada.ru/activity/' in url and url.endswith('/tasks'):
                return url
    if event_id.startswith('msu-'):
        return 'https://olymp.msu.ru/rus/page/main/29/page/zadaniya-olimpiady-proshlyh-let'
    if event_id == 'hse-international-youth':
        return 'https://olymp.hse.ru/inter/previous/'
    if event_id == 'nto-school':
        return 'https://ntcontest.ru/books/'
    if event_id == 'urfu-olymps-vuzakadem-index':
        return 'https://dovuz.urfu.ru/olymps/vuzakadem/materials'
    return None


def knopki_glavnoy():
  rows = []
  rows.append([knopka(*MENU[0])])
  for i in (1, 3, 5):
       rows.append([knopka(*MENU[i]), knopka(*MENU[i + 1])])
  return rows


def knopki_ekrana(rows=None, *, back=None):
     out = list(rows or [])
     if back:
          out.append([knopka('← Назад', back)])
     return out


def profil_gotov(user):
     return bool(user and user['age'] and user['grade'] and user['region'] and user['interests'])


def sostoyanie(uid):
    if uid not in STATE:
        session = get_ai_session(uid)
        STATE[uid] = ({'stage': 'assistant', 'ai_event': session['event_id'],
                       'history': session['history']} if session else {})
    return STATE[uid]


def spisok_sobitiy(uid, kind):
    if kind == 'favorites':
        events = []
        for eid in tracked_ids(uid, 'favorite'):
            event = get_event(eid)
            if event:
                events.append(event)
        return events
    user = get_user(uid)
    if profil_gotov(user):
        return recommended(user, segodnya())
    return []




def otbor_sobitiy(events, filtry, user):
    itog = []
    current_day = segodnya().isoformat()
    predmet = filtry.get('subject')
    tip = filtry.get('kind')
    for e in events:
        if predmet is not None and INTERESTS[predmet] not in e['subjects']:
            continue
        if tip is not None and e['kind'] != KINDS[tip]:
            continue
        dates = [e['registration_deadline'], e['result_date']]
        for stage in e['stages']:
            dates.append(stage.get('date'))
        future_date = False
        for day in dates:
            if day and day >= current_day:
                future_date = True
        if filtry.get('due') and not future_date:
            continue
        if filtry.get('fit') and user and not matches(e, user):
            continue
        itog.append(e)
    itog.sort(key=lambda event: (event['registration_deadline'] is None,
                                 event['registration_deadline'] or '9999-12-31', event['title']))
    return itog


API = None
EDIT_TARGET = ContextVar('edit_target', default=None)


async def send(uid, text, rows=None, image=None, *, edit=None, extra=None):
    current = EDIT_TARGET.get()
    target = edit or (current[1] if current and current[0] == uid else None)
    if target and extra is None:
        edited = False
        result = None
        with suppress(MaxAPIError, aiohttp.ClientError, asyncio.TimeoutError):
            result = await API.edit(target, text, rows, BANNERS.get(image) if image else None)
            edited = True
        if edited:
            return result
    return await API.send(uid, text, rows, BANNERS.get(image) if image else None, extra)

async def home(uid):
    sostoyanie(uid)['stage'] = None
    user = get_user(uid)
    if not profil_gotov(user):
        return await number(uid, 'age', setup=True)
    text = ('<b>🧭 ОлимпНавигатор</b>\n\n'
            '📚 <i>Находи</i> олимпиады, конкурсы и программы.\n'
            '🔎 <i>Фильтруй</i> подборку по предмету, типу и срокам.\n'
            '⭐ <i>Сохраняй</i> интересное и следи за датами.\n'
            '🏅 <i>Собирай</i> достижения и участвуй в рейтинге.\n'
            '🤖 Задавай вопросы GigaChat.\n\n'
            '<i>Выбери раздел ниже 👇</i>')
    return await send(uid, text, knopki_glavnoy(), 'welcome')

async def number(uid, field, setup=False, edit=None):
    st = sostoyanie(uid)
    st['mode'] = 'setup' if setup else 'edit'
    st['stage'] = field
    chisla = range(8, 19) if field == 'age' else range(4, 12)
    seleted = st.get('selected_' + field)
    if seleted is None:
        seleted = (get_user(uid) or {}).get(field)
    nazvanie = 'возраст' if field == 'age' else 'класс'
    rows = []
    row = []
    for x in chisla:
        label = str(x)
        if seleted == x:
            label = '✅ ' + label
        row.append(knopka(label, f'pick:{field}:{x}'))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([knopka('✅ Подтвердить', f'confirm:{field}')])
    return await send(uid, f'<b>🎓 Выбери {nazvanie}</b>\n\nНажми вариант и подтверди 👇',
                           knopki_ekrana(rows, back='profile' if not setup else ('home' if field == 'grade' else None)), edit=edit)

async def regions(uid, page=0, edit=None):
    st = sostoyanie(uid)
    st['stage'] = 'region'
    max_paje = (len(REGIONS) - 1) // REGION_PAGE
    page = max(0, min(page, max_paje))
    st['region_page'] = page
    vybranniy = st.get('selected_region') or (get_user(uid) or {}).get('region')
    rows = []
    for i in range(page * REGION_PAGE, min((page + 1) * REGION_PAGE, len(REGIONS))):
        label = REGIONS[i][:55]
        if vybranniy == REGIONS[i]:
            label = '✅ ' + label
        rows.append([knopka(label, f'region_pick:{i}')])
    rows.append([knopka('←', f'region_page:{page-1}'), knopka(f'{page+1}/{max_paje+1}', 'noop'),
                 knopka('→', f'region_page:{page+1}')])
    rows.append([knopka('✅ Подтвердить', 'region_confirm')])
    back = 'profile' if st.get('mode') == 'edit' else 'grade_back'
    return await send(uid, '<b>📍 Регион</b>\n\nВыбери регион и подтверди 👇',
                           knopki_ekrana(rows, back=back), edit=edit)

async def interests(uid, edit=None):
    st = sostoyanie(uid)
    st['stage'] = 'interests'
    selected = st.get('selected_interests')
    if selected is None:
        selected = (get_user(uid) or {}).get('interests', [])
        st['selected_interests'] = selected
    rows = []
    for i in range(0, len(INTERESTS), 2):
        row = []
        for j in range(i, min(i + 2, len(INTERESTS))):
            icon = '✅ ' if INTERESTS[j] in selected else '▫️ '
            row.append(knopka(icon + INTERESTS[j], f'interest:{j}'))
        rows.append(row)
    rows.append([knopka('✅ Подтвердить', 'interests_done')])
    back = 'profile' if st.get('mode') == 'edit' else 'region_back'
    return await send(uid, '<b>🎯 Интересы</b>\n\nВыбери направления. Можно несколько ✨',
                           knopki_ekrana(rows, back=back), edit=edit)

async def profile(uid):
    user = get_user(uid)
    if not user:
        return await home(uid)
    text = (f'<b>👤 Мой профиль</b>\n\n🎂 <b>Возраст:</b> {user["age"] or "-"} · '
            f'🎓 <b>Класс:</b> {user["grade"] or "-"}\n\n'
            f'📍 <b>Регион:</b> {bezopasniy_tekst(user["region"] or "-")}\n\n'
            f'🎯 <b>Интересы:</b> {bezopasniy_tekst(", ".join(user["interests"]) or "-")}\n\n'
            f'🏫 <b>Школа:</b> {bezopasniy_tekst(user["school"] or "-")}\n'
            f'🔔 <b>Уведомления:</b> {"включены ✅" if user["notifications"] else "выключены ⏸️"}\n'
            f'🤝 <b>Поиск напарника:</b> {"виден другим ✅" if user["partner_public"] else "скрыт 🔒"}')
    rows = [[knopka('🎂 Возраст', 'field:age'), knopka('🎓 Класс', 'field:grade')],
            [knopka('📍 Регион', 'field:region'), knopka('🎯 Интересы', 'field:interests')],
            [knopka('🏫 Изменить школу', 'school'), knopka('🔔 Уведомления', 'notifications')],
            [knopka('🤝 Найти напарника', 'partner_search')],
            [knopka('⚙️ Настройки поиска', 'partner_settings')],
            [ssylka('💬 Поддержка', SUPPORT_URL) if SUPPORT_URL else knopka('💬 Поддержка', 'support')],
            [knopka('🗑 Удалить мои данные', 'delete_prompt')]]
    return await send(uid, text, knopki_ekrana(rows, back='home'), 'profile')

async def partner_settings(uid, edit=None):
    user = get_user(uid)
    visible = bool(user and user['partner_public'])
    text = ('<b>⚙️ Настройки поиска напарника</b>\n\n'
            'Когда поиск включён, другие участники увидят твоё имя из MAX, '
            'класс, интересы и школу. Они смогут написать тебе через бота.\n\n'
            f'<b>Сейчас:</b> {"можно найти и написать ✅" if visible else "профиль скрыт 🔒"}')
    rows = [[knopka('🔒 Скрыть меня' if visible else '✅ Разрешить писать мне', 'partner_toggle')]]
    return await send(uid, text, knopki_ekrana(rows, back='profile'), 'profile', edit=edit)


async def partner_filters(uid, edit=None):
    filters = sostoyanie(uid).setdefault('partner_filters', {'grade': None, 'interests': []})
    grade = filters['grade']
    interests = filters['interests']
    text = ('<b>🤝 Найти напарника</b>\n\n'
            'Сначала выбери класс и интересы, затем нажми «Показать».\n\n'
            f'🎓 <b>Класс:</b> {grade or "любой"}\n'
            f'🎯 <b>Интересы:</b> {bezopasniy_tekst(", ".join(interests) or "любые")}\n\n'
            '<i>Если выбрано несколько интересов, подойдёт совпадение хотя бы по одному. '
            'В списке только те, кто разрешил писать себе.</i>')
    rows = [[knopka('🎓 Выбрать класс', 'partner_grades')],
            [knopka('🎯 Выбрать интересы', 'partner_interests')],
            [knopka('🔎 Показать участников', 'partner_show')]]
    return await send(uid, text, knopki_ekrana(rows, back='profile'), 'profile', edit=edit)


async def partner_grades(uid, edit=None):
    chosen = sostoyanie(uid).setdefault('partner_filters', {'grade': None, 'interests': []})['grade']
    rows = []
    for grade in range(4, 12, 2):
        rows.append([knopka(('✅ ' if chosen == number else '') + f'{number} класс',
                            f'partner_grade:{number}') for number in (grade, grade + 1)])
    rows.append([knopka('Любой класс', 'partner_grade:all')])
    return await send(uid, '<b>🎓 Класс напарника</b>\n\nВыбери один класс или любой 👇',
                      knopki_ekrana(rows, back='partner_search'), 'profile', edit=edit)


async def partner_interests(uid, edit=None):
    filters = sostoyanie(uid).setdefault('partner_filters', {'grade': None, 'interests': []})
    chosen = filters['interests']
    rows = []
    for i in range(0, len(INTERESTS), 2):
        rows.append([knopka(('✅ ' if INTERESTS[j] in chosen else '▫️ ') + INTERESTS[j],
                            f'partner_interest:{j}') for j in range(i, min(i + 2, len(INTERESTS)))])
    rows.append([knopka('Мои интересы', 'partner_interest:mine'),
                 knopka('Сбросить', 'partner_interest:clear')])
    rows.append([knopka('✅ Готово', 'partner_search')])
    return await send(uid, '<b>🎯 Интересы напарника</b>\n\nОтметь нужные направления 👇',
                      knopki_ekrana(rows, back='partner_search'), 'profile', edit=edit)


async def partner_results(uid, page=0, edit=None):
    filters = sostoyanie(uid).setdefault('partner_filters', {'grade': None, 'interests': []})
    own = get_user(uid) or {}
    own_interests = set(own.get('interests', []))
    wanted = set(filters['interests'])
    candidates = partner_users(uid)
    people = [user for user in candidates
              if (filters['grade'] is None or user['grade'] == filters['grade'])
              and (not wanted or wanted.intersection(user['interests']))]
    people.sort(key=lambda user: (-len(own_interests.intersection(user['interests'])),
                                   abs((own.get('grade') or user['grade']) - user['grade']),
                                   (user['display_name'] or '').casefold()))
    last = max(0, (len(people) - 1) // PAGE)
    page = max(0, min(page, last))
    sostoyanie(uid)['partner_page'] = page
    rows = [[knopka(f'👤 {(user["display_name"] or "Участник")[:40]} · {user["grade"]} кл.',
                    f'partner_person:{user["tg_id"]}')]
            for user in people[page * PAGE:(page + 1) * PAGE]]
    if len(people) > PAGE:
        rows.append([knopka('←', f'partner_page:{page-1}'),
                     knopka(f'{page+1}/{last+1}', 'noop'),
                     knopka('→', f'partner_page:{page+1}')])
    body = (f'Нашлось участников: <b>{len(people)}</b>\n\nВыбери человека 👇' if people else
            'По таким фильтрам никого не нашлось 😔\n\nПопробуй изменить класс или интересы.' if candidates else
            'Пока никто не разрешил писать себе через бота 😔\n\nЗагляни сюда позже.')
    return await send(uid, f'<b>🤝 Напарники</b>\n\n{body}',
                      knopki_ekrana(rows, back='partner_search'), 'profile', edit=edit)


async def partner_person(uid, person_id, edit=None):
    person = get_user(person_id)
    if person_id == uid or not person or not person['partner_public'] or not person['grade']:
        return await partner_results(uid, sostoyanie(uid).get('partner_page', 0))
    text = (f'<b>👤 {bezopasniy_tekst(person["display_name"] or "Участник")}</b>\n\n'
            f'🎓 <b>Класс:</b> {person["grade"]}\n'
            f'🎯 <b>Интересы:</b> {bezopasniy_tekst(", ".join(person["interests"]) or "не указаны")}\n'
            f'🏫 <b>Школа:</b> {bezopasniy_tekst(person["school"] or "не указана")}\n\n'
            '<i>Сообщение придёт через ОлимпНавигатор.</i>')
    own = get_user(uid)
    rows = [[knopka('✉️ Написать', f'partner_write:{person_id}')]] if own and own['partner_public'] else [
            [knopka('🔒 Включить сообщения у себя', 'partner_settings')]]
    return await send(uid, text, knopki_ekrana(rows, back=f'partner_page:{sostoyanie(uid).get("partner_page", 0)}'),
                      'profile', edit=edit)


async def partner_write(uid, person_id):
    own = get_user(uid)
    person = get_user(person_id)
    if not own or not own['partner_public']:
        return await partner_settings(uid)
    if person_id == uid or not person or not person['partner_public']:
        return await partner_results(uid, sostoyanie(uid).get('partner_page', 0))
    st = sostoyanie(uid)
    st['stage'] = 'partner_message'
    st['partner_recipient'] = person_id
    return await API.send(uid, f'<b>✉️ Сообщение для {bezopasniy_tekst(person["display_name"] or "участника")}</b>\n\n'
                               'Напиши сообщение одним текстом. До 700 символов.\n'
                               '<i>Можно отправить до 5 сообщений одному человеку и до 20 всего за день.</i>',
                          knopki_ekrana(back=f'partner_person:{person_id}'))

async def listing(uid, events, kind='events', page=0, edit=None):
    st = sostoyanie(uid)
    st['list_ids'] = [e['id'] for e in events[:500]]
    st['list_kind'] = kind
    return await render_list(uid, page, edit)

async def render_list(uid, page=0, edit=None):
    st = sostoyanie(uid)
    ids = st.get('list_ids', [])
    kind = st.get('list_kind', 'events')
    max_paje = max(0, (len(ids) - 1) // PAGE)
    page = max(0, min(page, max_paje))
    st['list_page'] = page
    zagolovok = '⭐ Избранное' if kind == 'favorites' else '📚 Мероприятия'
    rows = []
    for eid in ids[page * PAGE:(page + 1) * PAGE]:
        e = get_event(eid)
        if e:
            rows.append([knopka(e['title'][:55] + ('…' if len(e['title']) > 55 else ''), f'open:{eid}')])
    if ids:
        rows.append([knopka('←', f'page:{page-1}'), knopka(f'{page+1}/{max_paje+1}', 'noop'),
                     knopka('→', f'page:{page+1}')])
    if kind in ('favorites', 'search'):
        rows.append([knopka('⚙️ Фильтры', 'filters')])
    text = (f'<b>{zagolovok}</b>\n\n🔢 Найдено: <b>{len(ids)}</b>\n\n'
            '<i>👆 Нажми на событие, чтобы открыть карточку 🖼️</i>' if ids else
            f'<b>{zagolovok}</b>\n\n<i>По выбранным условиям пока ничего нет.</i>\n\n⚙️ Попробуй изменить фильтры ✨')
    return await send(uid, text, knopki_ekrana(rows, back='home'),
                           kind if kind in BANNERS else 'events', edit=edit)

async def card(uid, eid, edit=None):
    e = get_event(eid)
    if not e:
        return await home(uid)
    stage = None
    for item in e['stages']:
        if item.get('date') and item['date'] >= segodnya().isoformat():
            stage = item
            break
    level = {1: 'I уровень', 2: 'II уровень', 3: 'III уровень', 0: 'без уровня'}.get(e.get('rsosh_level'), 'не подтверждён')
    grade_text = f'{min(e["grades"])}-{max(e["grades"])}' if e['grades'] else 'уточняйте'
    text = (f'<b>🏆 {bezopasniy_tekst(e["title"][:100])}</b>\n\n'
            f'📚 <b>{bezopasniy_tekst(e["kind"].capitalize())}</b> · {bezopasniy_tekst(", ".join(e["subjects"][:3]))}\n'
            f'🎖️ РСОШ: <b>{level}</b>\n\n'
            f'🎓 <b>Классы:</b> {grade_text}\n'
            f'🌐 <b>Формат:</b> {bezopasniy_tekst(e["format"][:45])}\n\n'
            f'⏰ Регистрация до: <b>{bezopasniy_tekst(e["registration_deadline"] or "уточняйте")}</b>\n'
            f'📅 Ближайший этап: {bezopasniy_tekst(stage["name"] + " · " + stage["date"] if stage else "даты уточняются")}\n\n'
            f'<i>{bezopasniy_tekst(e["description"][:260])}</i>\n\n'
            f'<b>👥 Кому подходит:</b> {bezopasniy_tekst(e["eligibility"][:100])}\n\n'
            f'<b>🎁 Призы и льготы:</b> {bezopasniy_tekst(e["benefits"][:100])}\n\n'
            f'✅ Проверено: {bezopasniy_tekst(e["checked_at"])}')
    tracked = is_tracked(uid, eid)
    rows = [[ssylka('🌐 Открыть источник', e['official_url'])],
            [knopka('📅 Добавить в календарь', f'cal:event:{eid}')],
            [knopka('🔔 Отслеживаю' if tracked['participating'] else '🔔 Следить', f'toggle:part:{eid}'),
             knopka('⭐ Сохранено' if tracked['favorite'] else '⭐ Сохранить', f'toggle:fav:{eid}')],
            [knopka('📖 Подробнее', f'more:{eid}')]]
    zadaniya = zadaniya_proshlyh_let(e)
    rows.insert(1, [ssylka('📚 Задания прошлых лет', zadaniya)] if zadaniya else
                [knopka('📚 Задания прошлых лет', f'tasks:{eid}')])
    return await send(uid, text, knopki_ekrana(rows, back='backlist'), 'events', edit=edit)

async def tasks(uid, eid):
    e = get_event(eid)
    if not e:
        return await home(uid)
    text = (f'<b>📚 Задания прошлых лет</b>\n\n'
            f'<b>{bezopasniy_tekst(e["title"][:100])}</b>\n\n'
            'Пока не нашёл отдельный архив заданий для этого мероприятия. '
            'Проверь материалы на сайте организатора.')
    rows = [[ssylka('🌐 Сайт мероприятия', e['official_url'])]]
    return await send(uid, text, knopki_ekrana(rows, back=f'open:{eid}'), 'events')

async def more(uid, eid):
    e = get_event(eid)
    if not e:
        return await card(uid, eid)
    level = e.get('rsosh_level')
    points = POINTS[level if level in (1, 2, 3) else 0]
    stages = '\n'.join(f'• {bezopasniy_tekst(s["name"])}: {bezopasniy_tekst(s.get("date") or "дата уточняется")}'
                       for s in e['stages'][:8]) or 'Не опубликованы'
    preparation = '\n'.join(f'• <a href="{bezopasniy_tekst(item["url"])}">{bezopasniy_tekst(item["title"])}</a>'
                            for item in e['preparation'][:5]) or 'Смотрите сайт события'
    text = (f'<b>📖 {bezopasniy_tekst(e["title"][:100])}</b>\n\n'
            f'<b>📅 Этапы</b>\n{stages}\n\n'
            f'<b>📚 Подготовка</b>\n{preparation}\n\n'
            f'<b>👥 Условия</b>\n{bezopasniy_tekst(e["eligibility"])}\n\n'
            f'<b>🏅 Результаты</b>\n{bezopasniy_tekst(e["result_date"] or "дата не опубликована")}\n\n'
            f'🪙 <b>ОлимпКоины за результат:</b> победитель {points["победитель"]}, '
            f'призёр {points["призёр"]}, участник {points["участник"]}.\n'
            '<i>Начисляются после проверки достижения.</i>\n\n'
            f'<a href="{bezopasniy_tekst(e["source_url"])}">Проверить первоисточник</a> · '
            f'<a href="{RSOSH_SOURCE_URL}">Перечень РСОШ</a>')
    rows = [[knopka('🤖 Спросить ИИ об этом', f'assistant_event:{eid}')]]
    if ADMIN_IDS:
        rows.insert(0, [knopka('🏅 Добавить результат', f'result_begin:{eid}')])
    return await send(uid, text, knopki_ekrana(rows, back=f'open:{eid}'), 'events')

def knopki_filtrov(uid):
    o = sostoyanie(uid).setdefault('filters', {})
    subject = INTERESTS[o['subject']] if isinstance(o.get('subject'), int) and 0 <= o['subject'] < len(INTERESTS) else 'любой'
    kind = KINDS[o['kind']] if isinstance(o.get('kind'), int) and 0 <= o['kind'] < len(KINDS) else 'любой'
    return knopki_ekrana([
        [knopka(f'🎯 Предмет: {subject}', 'filter_subjects')],
        [knopka(f'🏆 Тип: {kind}', 'filter_kinds')],
        [knopka(('✅ ' if o.get('due') else '▫️ ') + 'Будущие сроки', 'fdue'),
         knopka(('✅ ' if o.get('fit') else '▫️ ') + 'Подходит мне', 'ffit')],
        [knopka('🔎 Показать', 'fapply'), knopka('🧹 Сбросить', 'freset')],
    ], back='favorites' if o.get('scope') == 'favorites' else 'home')


def knopki_predmetov(uid):
    o = sostoyanie(uid).setdefault('filters', {})
    rows = []
    for i in range(0, len(INTERESTS), 2):
        row = []
        for j in range(i, min(i + 2, len(INTERESTS))):
            icon = '✅ ' if o.get('subject') == j else '▫️ '
            row.append(knopka(icon + INTERESTS[j], f'fsub:{j}'))
        rows.append(row)
    rows.append([knopka('🧹 Любой предмет', 'fsub:all')])
    return knopki_ekrana(rows, back='filters')


def knopki_tipov(uid):
    o = sostoyanie(uid).setdefault('filters', {})
    rows = []
    for i in range(len(KINDS)):
        icon = '✅ ' if o.get('kind') == i else '▫️ '
        rows.append([knopka(icon + KINDS[i].capitalize(), f'fkind:{i}')])
    rows.append([knopka('🧹 Любой тип', 'fkind:all')])
    return knopki_ekrana(rows, back='filters')

async def filters(uid, scope='search', edit=None):
    o = sostoyanie(uid).setdefault('filters', {})
    o['scope'] = scope
    title = '⭐ Фильтры избранного' if scope == 'favorites' else '🔎 Поиск мероприятий'
    return await send(uid, f'<b>{title}</b>\n\n🎯 <b>Предмет</b> · 🏆 <b>тип события</b> · ⏰ <b>сроки</b>\n\n'
                                '<i>Отметь нужное и нажми «Показать» 👇</i>',
                           knopki_filtrov(uid), scope if scope in BANNERS else 'search', edit=edit)


async def filter_subjects(uid, edit=None):
    return await send(uid, '<b>🎯 Выбери предмет</b>\n\nМожно выбрать один предмет.',
                      knopki_predmetov(uid), 'search', edit=edit)


async def filter_kinds(uid, edit=None):
    return await send(uid, '<b>🏆 Выбери тип мероприятия</b>',
                      knopki_tipov(uid), 'search', edit=edit)

def sobytiya_kalendarya(events, today):
    found = []
    seen = set()
    for e in events:
        dates = [(e['registration_deadline'], 'Регистрация до'),
                 (e['result_date'], 'Результаты')]
        dates.extend((stage.get('date'), stage['name']) for stage in e['stages'])
        for day, label in dates:
            key = (e['id'], day, label)
            if day and day >= today.isoformat() and key not in seen:
                seen.add(key)
                found.append((day, label, e))
    return sorted(found, key=lambda item: (item[0], item[2]['title'], item[1]))


def ekran_ics(value):
    return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,')


def slozhit_ics_line(line):
    parts = []
    part = ''
    for char in line:
        if len((part + char).encode('utf-8')) > 75:
            parts.append(part)
            part = ' ' + char
        else:
            part += char
    parts.append(part)
    return '\r\n'.join(parts)


def kalendar_ics(items):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//OlimpNavigator//MAX//RU',
             'CALSCALE:GREGORIAN', 'METHOD:PUBLISH', 'X-WR-CALNAME:ОлимпНавигатор']
    for day, label, e in items:
        uid = hashlib.sha256(f'{e["id"]}|{day}|{label}'.encode('utf-8')).hexdigest()[:32]
        start = date.fromisoformat(day)
        lines.extend(['BEGIN:VEVENT', f'UID:{uid}@olimpnavigator', f'DTSTAMP:{stamp}',
                      f'DTSTART;VALUE=DATE:{start:%Y%m%d}',
                      f'DTEND;VALUE=DATE:{start + timedelta(days=1):%Y%m%d}',
                      f'SUMMARY:{ekran_ics(label + ": " + e["title"])}',
                      f'DESCRIPTION:{ekran_ics("Уточни время на сайте организатора: " + e["official_url"])}',
                      f'URL:{e["official_url"]}',
                      'BEGIN:VALARM', 'ACTION:DISPLAY',
                      f'DESCRIPTION:{ekran_ics(label + ": " + e["title"])}',
                      'TRIGGER:-P1D', 'END:VALARM', 'END:VEVENT'])
    lines.append('END:VCALENDAR')
    return ('\r\n'.join(slozhit_ics_line(line) for line in lines) + '\r\n').encode('utf-8')


async def otpravit_kalendar(uid, events, back):
    items = sobytiya_kalendarya(events, segodnya())
    if not items:
        return await send(uid, '📅 Для этого выбора пока нет будущих дат. Проверь карточку события позже.',
                          knopki_ekrana(back=back))
    with tempfile.TemporaryDirectory() as folder:
        file = Path(folder) / 'olimpnavigator.ics'
        file.write_bytes(kalendar_ics(items))
        token = await API.upload(file, 'file')
        await asyncio.sleep(1)
        return await send(uid, f'📅 <b>Календарь готов</b> · {len(items)} дат\n\n'
                               'Открой файл и добавь события в календарь телефона. '
                               'Напоминание стоит за день до каждой даты.',
                          knopki_ekrana(back=back), extra=[{'type': 'file', 'payload': {'token': token}}])


async def deadlines(uid):
    events = [get_event(eid) for eid in tracked_ids(uid, 'participating')]
    events = [e for e in events if e]
    lines = sobytiya_kalendarya(events, segodnya())
    body = '\n'.join(f'📌 <b>{bezopasniy_tekst(day)}</b> · {bezopasniy_tekst(label)}: {bezopasniy_tekst(e["title"][:70])}'
                     for day, label, e in lines[:15]) or '<i>Пока нет отслеживаемых дат.</i>'
    rows = [[knopka('📅 Добавить все в календарь', 'cal:all')]] if lines else []
    shown = set()
    for _, _, e in lines[:15]:
        if e['id'] not in shown:
            shown.add(e['id'])
            rows.append([knopka('📅 ' + e['title'][:40], f'cal:event:{e["id"]}')])
    return await send(uid, f'<b>⏰ Мои сроки</b>\n\n{body}\n\n'
                                '<i>Добавь мероприятие через кнопку «Следить» в его карточке.</i>',
                           knopki_ekrana(rows, back='home'), 'deadlines')

async def portfolio(uid):
    sostoyanie(uid)['stage'] = None
    rows = achievements_for(uid)
    statuses = {'pending': 'на проверке', 'approved': 'подтверждено',
                'rejected': 'отклонено', 'superseded': 'заменено новым результатом'}
    lines = []
    for row in rows[:15]:
        e = get_event(row['event_id'])
        lines.append(f'• <b>{bezopasniy_tekst((e or {}).get("title", row["event_id"])[:60])}</b> - '
                     f'{bezopasniy_tekst(row["result"])} <i>({statuses.get(row["status"], row["status"])}, ОлимпКоины: {row["points"]})</i>')
        if row['review_comment']:
            label = 'Причина отказа' if row['status'] == 'rejected' else 'Комментарий модератора'
            lines.append(f'  💬 <b>{label}:</b> {bezopasniy_tekst(row["review_comment"])}')
    top = leaderboard()
    rating = '\n'.join(f'{i}. {bezopasniy_tekst(r["display_name"] or "Участник")} · 🪙 {r["points"]}'
                       for i, r in enumerate(top[:10], 1)) or '<i>Подтверждённых результатов пока нет.</i>'
    score_lines = '\n'.join(
        f'{label}: {POINTS[level]["победитель"]} / {POINTS[level]["призёр"]} / {POINTS[level]["участник"]}'
        for level, label in ((1, 'I уровень'), (2, 'II уровень'), (3, 'III уровень'),
                             (0, 'Без уровня / не подтверждён')))
    text = (f'<b>🏅 Моё портфолио</b>\n\n' + ('\n'.join(lines) or '<i>Пока нет результатов.</i>') +
            f'\n\n<b>🏆 Рейтинг по ОлимпКоинам</b>\n{rating}\n\n'
            f'<b>🪙 ОлимпКоины за подтверждённый результат</b>\n'
            f'Победитель / призёр / участник:\n{score_lines}\n\n'
            '<i>ОлимпКоины - внутренняя награда ОлимпНавигатора. '
            'Они начисляются после проверки достижения администратором и определяют место в рейтинге. '
            'Льгот при поступлении ОлимпКоины не дают.</i>')
    return await send(uid, text, knopki_ekrana([[knopka('➕ Добавить достижение', 'achievement_pick:0')]], back='home'), 'portfolio')


async def vibor_dostizheniya(uid, page=0):
    sostoyanie(uid)['stage'] = None
    events = sorted(all_events(), key=lambda event: event['title'].casefold())
    last = max(0, (len(events) - 1) // PAGE)
    page = max(0, min(page, last))
    rows = [[knopka(event['title'][:55], f'result_begin:{event["id"]}')]
            for event in events[page * PAGE:(page + 1) * PAGE]]
    rows.append([knopka('✍️ Написать свою олимпиаду или конкурс', 'achievement_custom')])
    if last:
        rows.append([knopka('←', f'achievement_pick:{page-1}'),
                     knopka(f'{page+1}/{last+1}', 'noop'),
                     knopka('→', f'achievement_pick:{page+1}')])
    return await send(uid, f'<b>➕ Добавить достижение</b>\n\n'
                           f'Выбери мероприятие из каталога: <b>{len(events)}</b> 🏆\n'
                           'Или добавь своё название 👇',
                      knopki_ekrana(rows, back='portfolio'), 'portfolio')

async def assistant(uid, eid=None):
    st = sostoyanie(uid)
    st['stage'] = 'assistant'
    st['ai_event'] = eid
    st['history'] = []
    save_ai_session(uid, eid)
    remaining = AI_DAILY_LIMIT - ai_messages_today(uid, segodnya().isoformat())
    return await send(uid, '<b>🤖 ОлимпНавигатор х ГигаЧат</b>\n\n'
                                '<b>Представляем ОлимпЧат!</b> ✨\n\n'
                                'Помогу найти олимпиаду или конкурс во всём каталоге, '
                                'разобраться с уровнями РСОШ, перейти на официальный сайт мероприятия или рассказать о нём. 🔎\n\n'
                                '<i>Просто напиши свой вопрос - я отвечу здесь, в диалоге.</i> 💬\n\n'
                                'Можно задать до <b>50 вопросов в день</b>. '
                                f'Сегодня осталось: <b>{remaining}</b>.\n\n'
                                'Когда закончишь, нажми «Завершить диалог» 👇',
                           [[knopka('❌ Завершить диалог', 'assistant_finish')]], 'assistant')

async def uvesti_o_reshenii(achievement_id):
    async with REVIEW_NOTICE_LOCK:
        row = pending_review_notice(achievement_id)
        if not row:
            return
        event = get_event(row['event_id']) or {}
        title = bezopasniy_tekst(event.get('title', row['event_id']))
        comment = bezopasniy_tekst(row['review_comment']) if row['review_comment'] else ''
        if row['status'] == 'approved':
            text = (f'✅ Заявка на «{title}» одобрена!\n\n'
                    f'🏅 Результат: <b>{bezopasniy_tekst(row["result"])}</b>\n'
                    f'🪙 ОлимпКоины начислены: <b>{row["points"]}</b>. Место в рейтинге обновлено.')
            if comment:
                text += f'\n\n💬 <b>Комментарий модератора:</b> {comment}'
        else:
            text = (f'❌ Заявка на «{title}» отклонена.\n\n'
                    f'🪙 ОлимпКоины: <b>0</b>.\n'
                    f'💬 <b>Причина:</b> {comment}\n\n'
                    'Можно исправить подтверждение и отправить заявку снова.')
        await API.send(row['tg_id'], text + '\n\nСпасибо! 💜', knopki_ekrana(back='portfolio'))
        mark_review_notified(achievement_id)


async def zavershit_proverku(admin_id, comment):
    st = sostoyanie(admin_id)
    achievement_id = st.pop('review_id', None)
    approved = st.pop('review_approved', False)
    st['stage'] = st.pop('review_prev_stage', None)
    if achievement_id is None:
        return await send(admin_id, 'Заявка для проверки не выбрана.', knopki_ekrana(back='home'))
    row = review_achievement(achievement_id, admin_id, approved, comment)
    if not row:
        return await send(admin_id, 'Эта заявка уже проверена.', knopki_ekrana(back='home'))
    with suppress(MaxAPIError, aiohttp.ClientError, asyncio.TimeoutError):
        await uvesti_o_reshenii(achievement_id)
    label = 'одобрена' if approved else 'отклонена'
    buttons = ([[knopka('❌ Завершить диалог', 'assistant_finish')]] if st['stage'] == 'assistant'
               else knopki_ekrana(back='home'))
    return await send(admin_id, f'✅ Заявка #{achievement_id} {label}.', buttons)


async def uvedomit_adminov():
    for admin in ADMIN_IDS:
        for row in pending_reviews_for(admin):
            e = get_event(row['event_id'])
            with suppress(MaxAPIError, aiohttp.ClientError, asyncio.TimeoutError):
                dokazatelstvo = f'\n📎 Подтверждение: {bezopasniy_tekst(row["file_name"] or "без файла")}'
                extra = ([{'type': row['file_kind'], 'payload': {'token': row['file_id']}}]
                         if row['file_id'] and row['file_kind'] in ('image', 'file') else None)
                applicant = get_user(row['tg_id']) or {}
                await send(admin, f'<b>🏅 Проверка #{row["id"]}</b>\n\n'
                                       f'Пользователь: {bezopasniy_tekst(applicant.get("display_name") or row["tg_id"])}\n'
                                       f'Событие: {bezopasniy_tekst((e or {}).get("title", row["event_id"]))}\n'
                                       f'Результат: {bezopasniy_tekst(row["result"])}{dokazatelstvo}',
                                knopki_ekrana([[knopka('✅ Подтвердить', f'review:yes:{row["id"]}'),
                                         knopka('❌ Отклонить', f'review:no:{row["id"]}')]]), extra=extra)
                mark_admin_delivered(row['id'], admin)

async def sohranit_rezultat(uid, file_token=None, file_kind=None, file_name=None):
    st = sostoyanie(uid)
    eid, result = st.get('result_event'), st.get('result')
    if not eid or not result:
        return await home(uid)
    saved = False
    with suppress(sqlite3.IntegrityError):
        add_achievement(uid, eid, result, file_token, file_kind, file_name)
        saved = True
    if not saved:
        return await portfolio(uid)
    st['stage'] = None
    event = get_event(eid) or {}
    await send(uid, f'<b>✅ Заявка на «{bezopasniy_tekst(event.get("title", eid))}» отправлена на проверку.</b>\n\n'
                         '<i>После подтверждения ОлимпКоины появятся в рейтинге.</i>', knopki_ekrana(back='portfolio'))
    await uvedomit_adminov()

async def callback(uid, action, mid=None):
    target = None if action in ('home', 'assistant_finish') or not mid else (uid, mid)
    token = EDIT_TARGET.set(target)
    try:
        return await callback_action(uid, action, mid)
    finally:
        EDIT_TARGET.reset(token)


async def callback_action(uid, action, mid=None):
    st = sostoyanie(uid)
    if st.get('stage') == 'assistant' and action not in ('assistant_finish', 'noop') and not (uid in ADMIN_IDS and action.startswith('review:')):
        return await send(uid, '🤖 Сначала заверши диалог с GigaChat 👇',
                          [[knopka('❌ Завершить диалог', 'assistant_finish')]])
    if action.startswith('achievement_pick:'):
        return await vibor_dostizheniya(uid, int(action.partition(':')[2]))
    if action == 'achievement_custom':
        st['stage'] = None
        return await send(uid, '<b>✍️ Своё мероприятие</b>\n\nЧто ты хочешь добавить?',
                          knopki_ekrana([[knopka('🏆 Олимпиаду', 'achievement_kind:олимпиада'),
                                   knopka('🎯 Конкурс', 'achievement_kind:конкурс')]],
                                 back='achievement_pick:0'), 'portfolio')
    if action.startswith('achievement_kind:'):
        kind = action.partition(':')[2]
        if kind not in ('олимпиада', 'конкурс'):
            return await vibor_dostizheniya(uid)
        st['custom_kind'] = kind
        st['stage'] = 'custom_title'
        return await send(uid, f'<b>✍️ {kind.capitalize()}</b>\n\nНапиши название одним сообщением.',
                          knopki_ekrana(back='achievement_custom'), 'portfolio')
    if action == 'assistant_finish':
        end_ai_session(uid)
        st['stage'] = None
        st.pop('history', None)
        st.pop('ai_event', None)
        return await home(uid)
    sections = [item[1] for item in MENU]
    card_navigation = action.startswith(('page:', 'open:', 'more:', 'tasks:', 'cal:', 'toggle:', 'partner_'))
    if action in sections or action in ('backlist', 'filters') or card_navigation:
        st['stage'] = None
    if action == 'noop':
        return
    if action == 'home':
        return await home(uid)
    if action == 'events':
        return await listing(uid, spisok_sobitiy(uid, 'events'))
    if action == 'favorites':
        return await listing(uid, spisok_sobitiy(uid, 'favorites'), 'favorites')
    if action == 'search':
        st['filters'] = {'scope': 'search'}
        return await filters(uid)
    if action == 'filters':
        return await filters(uid, st.get('list_kind', 'search') if st.get('list_kind') == 'favorites' else 'search')
    if action == 'filter_subjects':
        return await filter_subjects(uid, mid)
    if action == 'filter_kinds':
        return await filter_kinds(uid, mid)
    if action == 'deadlines':
        return await deadlines(uid)
    if action == 'cal:all':
        events = [get_event(eid) for eid in tracked_ids(uid, 'participating')]
        return await otpravit_kalendar(uid, [e for e in events if e], 'deadlines')
    if action.startswith('cal:event:'):
        eid = action.removeprefix('cal:event:')
        e = get_event(eid)
        return await otpravit_kalendar(uid, [e] if e else [], f'open:{eid}')
    if action == 'profile':
        st['stage'] = None
        return await profile(uid)
    if action == 'partner_settings':
        return await partner_settings(uid)
    if action == 'partner_toggle':
        user = get_user(uid)
        update_user(uid, partner_public=0 if user and user['partner_public'] else 1)
        return await partner_settings(uid, mid)
    if action == 'partner_search':
        return await partner_filters(uid)
    if action == 'partner_grades':
        return await partner_grades(uid)
    if action.startswith('partner_grade:'):
        raw = action.partition(':')[2]
        if raw == 'all' or raw.isdigit() and 4 <= int(raw) <= 11:
            st.setdefault('partner_filters', {'grade': None, 'interests': []})['grade'] = None if raw == 'all' else int(raw)
        return await partner_filters(uid, mid)
    if action == 'partner_interests':
        return await partner_interests(uid)
    if action.startswith('partner_interest:'):
        raw = action.partition(':')[2]
        partner_filter_state = st.setdefault('partner_filters', {'grade': None, 'interests': []})
        if raw == 'mine':
            partner_filter_state['interests'] = (get_user(uid) or {}).get('interests', [])[:]
        elif raw == 'clear':
            partner_filter_state['interests'] = []
        elif raw.isdigit() and 0 <= int(raw) < len(INTERESTS):
            name = INTERESTS[int(raw)]
            partner_filter_state['interests'] = ([x for x in partner_filter_state['interests'] if x != name]
                                                 if name in partner_filter_state['interests']
                                                 else partner_filter_state['interests'] + [name])
        return await partner_interests(uid, mid)
    if action == 'partner_show':
        return await partner_results(uid)
    if action.startswith('partner_page:'):
        return await partner_results(uid, int(action.partition(':')[2]), mid)
    if action.startswith('partner_person:'):
        return await partner_person(uid, int(action.partition(':')[2]), mid)
    if action.startswith('partner_write:'):
        return await partner_write(uid, int(action.partition(':')[2]))
    if action == 'portfolio':
        return await portfolio(uid)
    if action == 'assistant':
        return await assistant(uid)
    if action.startswith('assistant_event:'):
        return await assistant(uid, action.partition(':')[2])
    if action == 'backlist':
        return await render_list(uid, st.get('list_page', 0))
    if action.startswith('page:'):
        return await render_list(uid, int(action.partition(':')[2]), mid)
    if action.startswith('open:'):
        return await card(uid, action.partition(':')[2])
    if action.startswith('more:'):
        return await more(uid, action.partition(':')[2])
    if action.startswith('tasks:'):
        return await tasks(uid, action.partition(':')[2])
    if action.startswith('toggle:'):
        _, what, eid = action.split(':', 2)
        if not get_event(eid):
            return await home(uid)
        current = is_tracked(uid, eid)
        field = 'favorite' if what == 'fav' else 'participating'
        track(uid, eid, field, not current[field])
        return await card(uid, eid, mid)
    if action.startswith('field:'):
        field = action.partition(':')[2]
        st['mode'] = 'edit'
        st['selected_' + field] = None
        if field in ('age', 'grade'):
            return await number(uid, field)
        if field == 'region':
            return await regions(uid)
        if field == 'interests':
            st['selected_interests'] = (get_user(uid) or {}).get('interests', [])
            return await interests(uid)
    if action.startswith('pick:'):
        _, field, raw = action.split(':')
        value = int(raw)
        if value in (range(8, 19) if field == 'age' else range(4, 12)):
            st['selected_' + field] = value
            return await number(uid, field, st.get('mode') == 'setup', mid)
    if action.startswith('confirm:'):
        field = action.partition(':')[2]
        value = st.get('selected_' + field) or (get_user(uid) or {}).get(field)
        if value is None:
            return await number(uid, field, st.get('mode') == 'setup')
        update_user(uid, **{field: value})
        if st.get('mode') == 'edit':
            st['stage'] = None
            return await profile(uid)
        return await (number(uid, 'grade', setup=True) if field == 'age' else regions(uid))
    if action == 'grade_back':
        return await number(uid, 'grade', setup=True)
    if action == 'region_back':
        return await regions(uid)
    if action.startswith('region_page:'):
        return await regions(uid, int(action.partition(':')[2]), mid)
    if action.startswith('region_pick:'):
        i = int(action.partition(':')[2])
        if 0 <= i < len(REGIONS):
            st['selected_region'] = REGIONS[i]
            return await regions(uid, st.get('region_page', 0), mid)
    if action == 'region_confirm':
        region = st.get('selected_region') or (get_user(uid) or {}).get('region')
        if not region:
            return await regions(uid)
        update_user(uid, region=region)
        if st.get('mode') == 'edit':
            st['stage'] = None
            return await profile(uid)
        st['selected_interests'] = []
        return await interests(uid)
    if action.startswith('interest:'):
        i = int(action.partition(':')[2])
        if 0 <= i < len(INTERESTS):
            selected = set(st.get('selected_interests', []))
            name = INTERESTS[i]
            selected.remove(name) if name in selected else selected.add(name)
            st['selected_interests'] = sorted(selected)
            return await interests(uid, mid)
    if action == 'interests_done':
        selected = st.get('selected_interests', [])
        if not selected:
            return await interests(uid)
        update_user(uid, interests=selected)
        st['stage'] = None
        if st.get('mode') == 'edit':
            return await profile(uid)
        return await listing(uid, spisok_sobitiy(uid, 'events'))
    if action.startswith(('fsub:', 'fkind:')) or action in ('fdue', 'ffit', 'freset'):
        o = st.setdefault('filters', {})
        if action == 'freset':
            scope = o.get('scope', 'search')
            st['filters'] = {'scope': scope}
        elif action in ('fdue', 'ffit'):
            key = 'due' if action == 'fdue' else 'fit'
            o[key] = not o.get(key, False)
        else:
            field, raw = action.split(':')
            key = 'subject' if field == 'fsub' else 'kind'
            if raw == 'all':
                o[key] = None
                return await (filter_subjects(uid, mid) if key == 'subject' else filter_kinds(uid, mid))
            i = int(raw)
            limit = len(INTERESTS) if key == 'subject' else len(KINDS)
            if not 0 <= i < limit:
                return
            o[key] = None if o.get(key) == i else i
        if action.startswith('fsub:'):
            return await filter_subjects(uid, mid)
        if action.startswith('fkind:'):
            return await filter_kinds(uid, mid)
        return await filters(uid, st['filters'].get('scope', 'search'), mid)
    if action == 'fapply':
        o = st.get('filters', {})
        scope = o.get('scope', 'search')
        events = spisok_sobitiy(uid, 'favorites') if scope == 'favorites' else all_events()
        return await listing(uid, otbor_sobitiy(events, o, get_user(uid)), scope)
    if action == 'notifications':
        user = get_user(uid)
        update_user(uid, notifications=not user['notifications'])
        return await profile(uid)
    if action == 'support':
        text = ('💬 <b>Поддержка</b>\n\nНапиши нам в MAX через кнопку ниже.'
                if SUPPORT_URL else f'💬 <b>Поддержка</b>\n\nID в MAX: {SUPPORT_ID or "не указан"}.')
        rows = [[ssylka('Написать в MAX', SUPPORT_URL)]] if SUPPORT_URL else []
        return await send(uid, text, knopki_ekrana(rows, back='profile'))
    if action == 'school':
        st['stage'] = 'school'
        return await send(uid, '<b>🏫 Школа</b>\n\nНапиши название школы одним сообщением.',
                               knopki_ekrana(back='profile'))
    if action == 'delete_prompt':
        return await send(uid, '<b>🗑 Удалить профиль?</b>\n\nЭто удалит избранное, сроки и достижения.',
                               knopki_ekrana([[knopka('Да, удалить', 'delete_yes')]], back='profile'))
    if action == 'delete_yes':
        from core import connect
        with connect() as db:
            db.execute('DELETE FROM sent_notifications WHERE tg_id=?', (uid,))
            db.execute('DELETE FROM users WHERE tg_id=?', (uid,))
        st.clear()
        return await send(uid, '✅ Данные удалены.', knopki_ekrana(back='home'))
    if action.startswith('result_begin:'):
        eid = action.partition(':')[2]
        event = get_event(eid)
        if not event or (event.get('custom') and event.get('owner_id') != uid):
            return await portfolio(uid)
        if not ADMIN_IDS:
            return await more(uid, eid)
        st['result_event'] = eid
        st['stage'] = None
        return await send(uid, '<b>🏅 Добавить результат</b>\n\nКакой у тебя результат?',
                               knopki_ekrana([[knopka('Участник', 'result:участник'), knopka('Призёр', 'result:призёр')],
                                       [knopka('Победитель', 'result:победитель')]], back='portfolio'))
    if action.startswith('result:'):
        result = action.partition(':')[2]
        if result not in ('участник', 'призёр', 'победитель') or not st.get('result_event'):
            return
        st['result'] = result
        st['stage'] = 'result_file'
        return await send(uid, '<b>📎 Подтверждение результата</b>\n\n'
                                    'Пришли диплом или сертификат файлом/фото. '
                                    'Если его нет, нажми «Без файла».',
                               knopki_ekrana([[knopka('Без файла', 'result_skip')]], back='portfolio'))
    if action == 'result_skip':
        return await sohranit_rezultat(uid)
    if action == 'review_cancel' and uid in ADMIN_IDS and st.get('stage') == 'review':
        st.pop('review_id', None)
        st.pop('review_approved', None)
        st['stage'] = st.pop('review_prev_stage', None)
        if st['stage'] == 'assistant':
            return await send(uid, 'Проверка отложена. Диалог с GigaChat продолжается.',
                              [[knopka('❌ Завершить диалог', 'assistant_finish')]])
        return await send(uid, 'Проверка отложена. Заявка остаётся на рассмотрении.',
                          knopki_ekrana(back='home'))
    if action == 'review_skip' and uid in ADMIN_IDS and st.get('stage') == 'review':
        if st.get('review_approved'):
            return await zavershit_proverku(uid, '')
    if action.startswith('review:') and uid in ADMIN_IDS:
        _, decision, raw = action.split(':')
        if decision not in ('yes', 'no'):
            return
        row = pending_achievement(int(raw))
        if not row:
            return await send(uid, 'Эта заявка уже проверена.', knopki_ekrana(back='home'))
        if st.get('stage') != 'review':
            st['review_prev_stage'] = st.get('stage')
        st['stage'] = 'review'
        st['review_id'] = row['id']
        st['review_approved'] = decision == 'yes'
        if decision == 'yes':
            return await API.send(uid, f'<b>✅ Одобрение заявки #{row["id"]}</b>\n\n'
                                       'Напиши комментарий для участника или нажми «Без комментария».',
                                  knopki_ekrana([[knopka('Без комментария', 'review_skip')]],
                                                back='review_cancel'))
        return await API.send(uid, f'<b>❌ Отклонение заявки #{row["id"]}</b>\n\n'
                                   'Напиши причину отказа одним сообщением. Она придёт участнику.',
                              knopki_ekrana(back='review_cancel'))
    return await send(uid, '🤔 Я ещё не знаю такой команды. Вернись на главную 👇',
                      knopki_ekrana(back='home'))

async def message(uid, message):
    body = message.get('body') or {}
    text = (body.get('text') or '').strip()
    attachments = body.get('attachments') or []
    st = sostoyanie(uid)
    if uid in ADMIN_IDS and st.get('stage') == 'review':
        if not text or text.startswith('/'):
            prompt = ('Напиши причину отказа одним сообщением.' if not st.get('review_approved')
                      else 'Напиши комментарий или нажми «Без комментария».')
            rows = [[knopka('Без комментария', 'review_skip')]] if st.get('review_approved') else []
            return await send(uid, prompt, knopki_ekrana(rows, back='review_cancel'))
        if len(' '.join(text.split())) > 1000:
            return await send(uid, 'Комментарий слишком длинный. Напиши до 1000 символов.',
                              knopki_ekrana(back='review_cancel'))
        return await zavershit_proverku(uid, text)
    if st.get('stage') == 'assistant':
        finish_button = [[knopka('❌ Завершить диалог', 'assistant_finish')]]
        if text == '/myid':
            return await send(uid, f'🪪 Твой MAX ID: <code>{uid}</code>', finish_button)
        if text in ('/start', 'старт', 'Старт') or not text:
            return await send(uid, '🤖 Диалог ещё открыт. Напиши вопрос или заверши его 👇',
                              finish_button)
        day = segodnya().isoformat()
        if not use_ai_message(uid, day, AI_DAILY_LIMIT):
            return await send(uid, '⏳ На сегодня закончились 50 вопросов к GigaChat. '
                                  'Завтра можно продолжить этот же диалог.\n\n'
                                  'Если хочешь вернуться в меню, заверши диалог 👇',
                              finish_button)
        user = get_user(uid)
        cards = kartochki_dlya_ii(text, st.get('history'), user, st.get('ai_event'))
        answer, provider = await ai_answer(text, cards, st.get('history'), segodnya().isoformat())
        st.setdefault('history', []).extend([{'role': 'user', 'text': text},
                                              {'role': 'assistant', 'text': answer}])
        st['history'] = st['history'][-20:]
        save_ai_session(uid, st.get('ai_event'), st['history'])
        title = ('🤖 Отвечает GigaChat' if provider == 'GigaChat'
                 else '📚 Справка по олимпиадам' if provider == 'reference'
                 else '📚 Подборка из каталога')
        remaining = AI_DAILY_LIMIT - ai_messages_today(uid, day)
        return await send(uid, f'<b>{title}</b>\n\n{tekst_ot_ii(answer)}\n\n'
                               f'<i>Осталось вопросов сегодня: {remaining} из {AI_DAILY_LIMIT}.</i>',
                               finish_button)
    if text in ('/start', 'старт', 'Старт'):
        return await home(uid)
    if text == '/myid':
        return await send(uid, f'🪪 Твой MAX ID: <code>{uid}</code>', knopki_ekrana(back='profile'))
    if st.get('stage') == 'partner_message':
        person_id = st.get('partner_recipient')
        if not text or text.startswith('/') or len(text) > 700:
            return await send(uid, '✉️ Напиши сообщение одним текстом до 700 символов.',
                              knopki_ekrana(back=f'partner_person:{person_id}'))
        status = reserve_partner_message(uid, person_id, segodnya().isoformat())
        if status != 'ok':
            st['stage'] = None
            info = ('Сегодня ты уже отправил этому участнику 5 сообщений. Попробуй завтра. ⏳' if status == 'limit'
                    else 'На сегодня закончились 20 сообщений. Попробуй завтра. ⏳' if status == 'daily_limit'
                    else 'Участник больше не принимает сообщения через бота. 😔')
            return await send(uid, info, knopki_ekrana(back='partner_search'))
        sender = get_user(uid) or {}
        await API.send(person_id, f'<b>🤝 Сообщение от {bezopasniy_tekst(sender.get("display_name") or "участника")}</b>\n\n'
                                  f'{bezopasniy_tekst(text)}\n\n'
                                  '<i>Чтобы ответить, нажми кнопку ниже. Сообщения передаются через ОлимпНавигатор.</i>',
                       knopki_ekrana([[knopka('↩️ Ответить', f'partner_write:{uid}'),
                                      knopka('🔒 Настройки поиска', 'partner_settings')]], back='profile'))
        st['stage'] = None
        st.pop('partner_recipient', None)
        return await send(uid, '✅ Сообщение отправлено напарнику.',
                          knopki_ekrana([[knopka('✉️ Написать ещё', f'partner_write:{person_id}')]],
                                        back=f'partner_person:{person_id}'))
    if text == '/import' and uid in ADMIN_IDS and attachments:
        file = next((a for a in attachments if a.get('type') == 'file'), None)
        url = ((file or {}).get('payload') or {}).get('url')
        if not url:
            return await home(uid)
        imported = None
        with suppress(aiohttp.ClientError, ValueError, UnicodeError, json.JSONDecodeError):
            async with API.session.get(url) as response:
                response.raise_for_status()
                data = await response.content.read(2_000_001)
            if len(data) > 2_000_000:
                raise ValueError('Файл больше 2 МБ')
            items = json.loads(data.decode('utf-8'))
            if not isinstance(items, list):
                raise ValueError('Ожидается массив карточек')
            imported = import_events(items)
        if imported is None:
            return await home(uid)
        added, updated, _ = imported
        return await send(uid, f'✅ Импортировано: новых {added}, обновлено {updated}.', knopki_ekrana(back='home'))
    if st.get('stage') == 'school' and text:
        update_user(uid, school=text[:150])
        st['stage'] = None
        return await profile(uid)
    if st.get('stage') == 'custom_title':
        if not text:
            return await send(uid, '✍️ Напиши название олимпиады или конкурса одним сообщением.',
                              knopki_ekrana(back='achievement_custom'))
        eid = create_custom_event(uid, text, st['custom_kind'])
        st['stage'] = None
        return await callback_action(uid, f'result_begin:{eid}')
    if st.get('stage') == 'result_file':
        for item in attachments:
            kind = item.get('type')
            if kind in ('image', 'file'):
                payload = item.get('payload') or {}
                token = payload.get('token')
                if token:
                    return await sohranit_rezultat(uid, token, kind,
                                                  payload.get('filename') or 'Подтверждение')
        return await send(uid, '📎 Пришли фото или файл. Можно выбрать «Без файла».',
                               knopki_ekrana([[knopka('Без файла', 'result_skip')]], back='portfolio'))
    if text.startswith('/import') and uid in ADMIN_IDS:
        return await send(uid, '📦 Пришли JSON-файл с подписью /import.',
                               knopki_ekrana(back='home'))
    if not profil_gotov(get_user(uid)):
        if st.get('stage') == 'grade':
            return await number(uid, 'grade', setup=True)
        if st.get('stage') == 'region':
            return await regions(uid, st.get('region_page', 0))
        if st.get('stage') == 'interests':
            return await interests(uid)
        return await number(uid, 'age', setup=True)
    return await send(uid, '🤔 Я ещё не знаю такой команды. Выбери раздел на главной 👇',
                      knopki_ekrana(back='home'))

async def obrabotat_sobytie(event):
    typ = event.get('update_type')
    if typ == 'bot_started':
        uid = (event.get('user') or {}).get('user_id')
        if uid:
            ensure_user(uid)
            remember_name(uid, event.get('user') or {})
            if sostoyanie(uid).get('stage') == 'assistant':
                await send(uid, '🤖 Диалог с GigaChat ещё открыт. Напиши вопрос или заверши его 👇',
                           [[knopka('❌ Завершить диалог', 'assistant_finish')]])
            else:
                await home(uid)
        return
    if typ == 'message_created':
        soobshenie = event.get('message') or {}
        if (soobshenie.get('recipient') or {}).get('chat_type') not in (None, 'dialog'):
            return
        sender = soobshenie.get('sender') or {}
        uid = sender.get('user_id')
        if uid and not sender.get('is_bot'):
            ensure_user(uid)
            remember_name(uid, sender)
            await message(uid, soobshenie)
        return
    if typ == 'message_callback':
        if ((event.get('message') or {}).get('recipient') or {}).get('chat_type') not in (None, 'dialog'):
            return
        sobytie_knopki = event.get('callback') or {}
        uid = (sobytie_knopki.get('user') or {}).get('user_id')
        if not uid:
            return
        ensure_user(uid)
        remember_name(uid, sobytie_knopki.get('user') or {})
        mid = ((event.get('message') or {}).get('body') or {}).get('mid')
        with suppress(MaxAPIError, aiohttp.ClientError, asyncio.TimeoutError):
            await API.answer(sobytie_knopki['callback_id'])
        await callback(uid, sobytie_knopki.get('payload') or '', mid)

async def raspisanie():
    while True:
        with suppress(Exception):
            now = datetime.now(ZoneInfo(TIMEZONE))
            if now.hour >= 9:
                for uid, e, key, label in due_notifications(now.date()):
                    with suppress(MaxAPIError, aiohttp.ClientError, asyncio.TimeoutError):
                        await send(uid, f'⏰ {bezopasniy_tekst(label)}: {bezopasniy_tekst(e["title"])}',
                                        knopki_ekrana([[knopka('Открыть', f'open:{e["id"]}')]]))
                        mark_sent(uid, e['id'], key)
            if ADMIN_IDS:
                await uvedomit_adminov()
            for row in pending_review_notifications():
                with suppress(MaxAPIError, aiohttp.ClientError, asyncio.TimeoutError):
                    await uvesti_o_reshenii(row['id'])
        await asyncio.sleep(900)


async def ezhednevniy_sbor():
    moskva = ZoneInfo('Europe/Moscow')
    while True:
        now = datetime.now(moskva)
        next_run = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        await asyncio.sleep((next_run - now).total_seconds())
        with suppress(Exception):
            events = await asyncio.to_thread(collect_all, next_run.date())
            if events:
                _, _, changes = import_events(events)
                for eid, label in changes:
                    e = get_event(eid)
                    if e:
                        for uid in subscribers_for(eid):
                            with suppress(MaxAPIError, aiohttp.ClientError, asyncio.TimeoutError):
                                await send(uid, f'🔔 Обновлено: {bezopasniy_tekst(label)} у «{bezopasniy_tekst(e["title"])}».',
                                               knopki_ekrana([[knopka('Открыть', f'open:{eid}')]]))


async def main():
    global API
    if not MAX_BOT_TOKEN:
        raise SystemExit
    logging.disable(logging.CRITICAL)
    init_db()
    start_events = await asyncio.to_thread(collect_all, datetime.now(ZoneInfo('Europe/Moscow')).date())
    if start_events:
        import_events(start_events)
    timeout = aiohttp.ClientTimeout(total=45)
    connector = aiohttp.TCPConnector(ssl=russian_ssl())
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        api = MaxAPI(MAX_BOT_TOKEN, session)
        API = api
        with suppress(MaxAPIError, aiohttp.ClientError, asyncio.TimeoutError):
            await api.request('PATCH', '/me/commands', payload={'commands': [
                {'name': 'start', 'description': 'Открыть главное меню'},
                {'name': 'myid', 'description': 'Узнать свой MAX ID'}]})
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(raspisanie())
            tasks.create_task(ezhednevniy_sbor())
            webhook_url = os.getenv('MAX_WEBHOOK_URL', '').strip()
            secret = os.getenv('MAX_WEBHOOK_SECRET', '').strip()
            if webhook_url and (not webhook_url.startswith('https://') or
                                not webhook_url.endswith('/max-webhook') or
                                not 5 <= len(secret) <= 256 or not secret.isascii() or
                                not all(c.isalnum() or c in '-_' for c in secret)):
                webhook_url = ''
            if webhook_url:
                async def receive(request):
                    if not hmac.compare_digest(request.headers.get('X-Max-Bot-Api-Secret', ''), secret):
                        raise web.HTTPForbidden()
                    event = await request.json()
                    await obrabotat_sobytie(event)
                    return web.json_response({'ok': True})
                server = web.Application()
                server.router.add_post('/max-webhook', receive)
                async with AsyncExitStack() as cleanup:
                    runner = web.AppRunner(server)
                    await runner.setup()
                    cleanup.push_async_callback(runner.cleanup)
                    site = web.TCPSite(runner, '0.0.0.0', int(os.getenv('PORT', '8080')))
                    await site.start()
                    subscription = await api.request('POST', '/subscriptions', payload={
                        'url': webhook_url,
                        'secret': secret,
                        'update_types': ['bot_started', 'message_created', 'message_callback']})
                    if subscription.get('success'):
                        print('Бот запущен', flush=True)
                        await asyncio.Event().wait()
            marker = None
            print('Бот запущен', flush=True)
            while True:
                data = None
                with suppress(MaxAPIError, aiohttp.ClientError, asyncio.TimeoutError):
                    data = await api.updates(marker)
                if data is None:
                    await asyncio.sleep(3)
                    continue
                for event in data.get('updates', []):
                    with suppress(Exception):
                            await obrabotat_sobytie(event)
                marker = data.get('marker', marker)


if __name__ == '__main__':
    asyncio.run(main())
