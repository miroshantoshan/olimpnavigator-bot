from __future__ import annotations
import asyncio
import html
import hmac
import importlib
import importlib.util
import json
import logging
import os
import re
import sqlite3
from contextlib import AsyncExitStack, suppress
from contextvars import ContextVar
from datetime import datetime
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
                  due_notifications, end_ai_session, ensure_user, get_ai_session, get_event, get_user, import_events,
                  init_db, is_tracked, leaderboard, mark_admin_delivered, mark_sent,
                  matches, pending_reviews_for, recommended, remember_name, review_achievement,
                  save_ai_session, subscribers_for, track, tracked_ids, update_user, use_ai_message)
from services import MaxAPI, MaxAPIError, ai_answer, russian_ssl
local_token = importlib.import_module('max_token') if importlib.util.find_spec('max_token') else None
MAX_BOT_TOKEN = getattr(local_token, 'MAX_BOT_TOKEN', os.getenv('MAX_BOT_TOKEN', ''))
ADMIN_IDS = getattr(local_token, 'ADMIN_IDS', [int(x) for x in os.getenv('MAX_ADMIN_IDS', '').split(',') if x.strip().isdigit()])
TIMEZONE = getattr(local_token, 'TIMEZONE', os.getenv('OLIMPNAV_TIMEZONE', 'Asia/Yekaterinburg'))
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
LOG = logging.getLogger(__name__)
PAGE = 6
REGION_PAGE = 12
AI_DAILY_LIMIT = 50
KINDS = ('олимпиада', 'конкурс', 'хакатон', 'образовательная программа')
MENU = [('📚 Мероприятия', 'events'), ('🔎 Поиск', 'search'), ('⭐ Избранное', 'favorites'),
        ('📅 Сроки', 'deadlines'), ('🏅 Портфолио', 'portfolio'),
        ('🤖 GigaChat', 'assistant'), ('👤 Профиль', 'profile')]
STATE: dict[int, dict] = {}


def esc(value):
  return html.escape(str(value or ''))


def ai_text(value):
    safe = esc(value)
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
                   'подробнее', 'есть', 'будет', 'для', 'него', 'ней'}


def ai_terms(text):
    words = re.findall(r'[a-zа-яё0-9]+', text.casefold().replace('ё', 'е'))
    return [word for word in words if len(word) > 2 and word not in AI_COMMON_WORDS]


def ai_cards(question, history, user, selected_id=None):
    if selected_id:
        card = get_event(selected_id)
        if card:
            return [card]
    events = all_events()
    previous = [turn.get('text', '') for turn in reversed(history or [])
                if turn.get('role') == 'user']
    for text in [question] + previous:
        terms = ai_terms(text)
        if not terms:
            continue
        scores = []
        for event in events:
            title = event['title'].casefold().replace('ё', 'е')
            details = (event['organizer'] + ' ' + event['description']).casefold().replace('ё', 'е')
            score = sum(6 if word in title else 1 if word in details else 0 for word in terms)
            if score:
                scores.append((score, event))
        if scores:
            scores.sort(key=lambda item: (-item[0], item[1]['title']))
            return [event for _, event in scores[:6]]
    return recommended(user, today())[:6] if ready(user) else events[:6]


def today():
      return datetime.now(ZoneInfo(TIMEZONE)).date()


def btn(label, value):
   return {'type': 'callback', 'text': label[:128], 'payload': value}


def link(label, url):
     return {'type': 'link', 'text': label[:128], 'url': url}


def nav():
    rows = []
    rows.append([btn(*MENU[0])])
    for i in (1, 3, 5):
        rows.append([btn(*MENU[i]), btn(*MENU[i + 1])])
    return rows


def screen(rows=None, *, back=None):
    out = list(rows or [])
    if back:
        out.append([btn('← Назад', back)])
    return out


def ready(user):
    return bool(user and user['age'] and user['grade'] and user['region'] and user['interests'])


def state(uid):
    if uid not in STATE:
        session = get_ai_session(uid)
        STATE[uid] = ({'stage': 'assistant', 'ai_event': session['event_id'],
                       'history': session['history']} if session else {})
    return STATE[uid]


def event_list(uid, kind):
    if kind == 'favorites':
        events = []
        for eid in tracked_ids(uid, 'favorite'):
            event = get_event(eid)
            if event:
                events.append(event)
        return events
    user = get_user(uid)
    if ready(user):
        return recommended(user, today())
    return []


def filter_events(events, filtry, user):
    itog = []
    current_day = today().isoformat()
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
    state(uid)['stage'] = None
    user = get_user(uid)
    if not ready(user):
        return await number(uid, 'age', setup=True)
    text = ('<b>🧭 ОлимпНавигатор</b>\n\n'
            '📚 Находи олимпиады, конкурсы и программы.\n'
            '🔎 Фильтруй подборку по предмету, типу и срокам.\n'
            '⭐ Сохраняй интересное и следи за датами.\n'
            '🏅 Собирай достижения и участвуй в рейтинге.\n'
            '🤖 Задавай вопросы GigaChat.\n\n'
            '<i>Выбери раздел ниже 👇</i>')
    return await send(uid, text, nav(), 'welcome')

async def number(uid, field, setup=False, edit=None):
    st = state(uid)
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
        row.append(btn(label, f'pick:{field}:{x}'))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn('✅ Подтвердить', f'confirm:{field}')])
    return await send(uid, f'<b>🎓 Выбери {nazvanie}</b>\n\nНажми вариант и подтверди 👇',
                           screen(rows, back='profile' if not setup else ('home' if field == 'grade' else None)), edit=edit)

async def regions(uid, page=0, edit=None):
    st = state(uid)
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
        rows.append([btn(label, f'region_pick:{i}')])
    rows.append([btn('←', f'region_page:{page-1}'), btn(f'{page+1}/{max_paje+1}', 'noop'),
                 btn('→', f'region_page:{page+1}')])
    rows.append([btn('✅ Подтвердить', 'region_confirm')])
    back = 'profile' if st.get('mode') == 'edit' else 'grade_back'
    return await send(uid, '<b>📍 Регион</b>\n\nВыбери регион и подтверди 👇',
                           screen(rows, back=back), edit=edit)

async def interests(uid, edit=None):
    st = state(uid)
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
            row.append(btn(icon + INTERESTS[j], f'interest:{j}'))
        rows.append(row)
    rows.append([btn('✅ Подтвердить', 'interests_done')])
    back = 'profile' if st.get('mode') == 'edit' else 'region_back'
    return await send(uid, '<b>🎯 Интересы</b>\n\nВыбери направления. Можно несколько ✨',
                           screen(rows, back=back), edit=edit)

async def profile(uid):
    user = get_user(uid)
    if not user:
        return await home(uid)
    text = (f'<b>👤 Мой профиль</b>\n\n🎂 <b>Возраст:</b> {user["age"] or "-"} · '
            f'🎓 <b>Класс:</b> {user["grade"] or "-"}\n\n'
            f'📍 <b>Регион:</b> {esc(user["region"] or "-")}\n\n'
            f'🎯 <b>Интересы:</b> {esc(", ".join(user["interests"]) or "-")}\n\n'
            f'🏫 <b>Школа:</b> {esc(user["school"] or "-")}\n'
            f'🔔 <b>Уведомления:</b> {"включены ✅" if user["notifications"] else "выключены ⏸️"}')
    rows = [[btn('🎂 Возраст', 'field:age'), btn('🎓 Класс', 'field:grade')],
            [btn('📍 Регион', 'field:region'), btn('🎯 Интересы', 'field:interests')],
            [btn('🏫 Изменить школу', 'school'), btn('🔔 Уведомления', 'notifications')],
            [btn('💬 Поддержка', 'support')], [btn('🗑 Удалить мои данные', 'delete_prompt')]]
    return await send(uid, text, screen(rows, back='home'), 'profile')

async def listing(uid, events, kind='events', page=0, edit=None):
    st = state(uid)
    st['list_ids'] = [e['id'] for e in events[:500]]
    st['list_kind'] = kind
    return await render_list(uid, page, edit)

async def render_list(uid, page=0, edit=None):
    st = state(uid)
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
            rows.append([btn(e['title'][:55] + ('…' if len(e['title']) > 55 else ''), f'open:{eid}')])
    if ids:
        rows.append([btn('←', f'page:{page-1}'), btn(f'{page+1}/{max_paje+1}', 'noop'),
                     btn('→', f'page:{page+1}')])
    if kind in ('favorites', 'search'):
        rows.append([btn('⚙️ Фильтры', 'filters')])
    text = (f'<b>{zagolovok}</b>\n\n🔢 Найдено: <b>{len(ids)}</b>\n\n'
            '<i>👆 Нажми на событие, чтобы открыть карточку 🖼️</i>' if ids else
            f'<b>{zagolovok}</b>\n\n<i>По выбранным условиям пока ничего нет.</i>\n\n⚙️ Попробуй изменить фильтры ✨')
    return await send(uid, text, screen(rows, back='home'),
                           kind if kind in BANNERS else 'events', edit=edit)

async def card(uid, eid, edit=None):
    e = get_event(eid)
    if not e:
        return await home(uid)
    stage = None
    for item in e['stages']:
        if item.get('date') and item['date'] >= today().isoformat():
            stage = item
            break
    level = {1: 'I уровень', 2: 'II уровень', 3: 'III уровень', 0: 'без уровня'}.get(e.get('rsosh_level'), 'не подтверждён')
    grade_text = f'{min(e["grades"])}-{max(e["grades"])}' if e['grades'] else 'уточняйте'
    text = (f'<b>🏆 {esc(e["title"][:100])}</b>\n\n'
            f'📚 <b>{esc(e["kind"].capitalize())}</b> · {esc(", ".join(e["subjects"][:3]))}\n'
            f'🎖️ РСОШ: <b>{level}</b>\n\n'
            f'🎓 <b>Классы:</b> {grade_text}\n'
            f'🌐 <b>Формат:</b> {esc(e["format"][:45])}\n\n'
            f'⏰ Регистрация до: <b>{esc(e["registration_deadline"] or "уточняйте")}</b>\n'
            f'📅 Ближайший этап: {esc(stage["name"] + " · " + stage["date"] if stage else "даты уточняются")}\n\n'
            f'<i>{esc(e["description"][:260])}</i>\n\n'
            f'<b>👥 Кому подходит:</b> {esc(e["eligibility"][:100])}\n\n'
            f'<b>🎁 Призы и льготы:</b> {esc(e["benefits"][:100])}\n\n'
            f'✅ Проверено: {esc(e["checked_at"])}')
    tracked = is_tracked(uid, eid)
    rows = [[link('🌐 Открыть источник', e['official_url'])],
            [btn('🔔 Отслеживаю' if tracked['participating'] else '🔔 Следить', f'toggle:part:{eid}'),
             btn('⭐ Сохранено' if tracked['favorite'] else '⭐ Сохранить', f'toggle:fav:{eid}')],
            [btn('📖 Подробнее', f'more:{eid}')]]
    return await send(uid, text, screen(rows, back='backlist'), 'events', edit=edit)

async def more(uid, eid):
    e = get_event(eid)
    if not e:
        return await card(uid, eid)
    level = e.get('rsosh_level')
    points = POINTS[level if level in (1, 2, 3) else 0]
    stages = '\n'.join(f'• {esc(s["name"])}: {esc(s.get("date") or "дата уточняется")}'
                       for s in e['stages'][:8]) or 'Не опубликованы'
    preparation = '\n'.join(f'• <a href="{esc(item["url"])}">{esc(item["title"])}</a>'
                            for item in e['preparation'][:5]) or 'Смотрите сайт события'
    text = (f'<b>📖 {esc(e["title"][:100])}</b>\n\n'
            f'<b>📅 Этапы</b>\n{stages}\n\n'
            f'<b>📚 Подготовка</b>\n{preparation}\n\n'
            f'<b>👥 Условия</b>\n{esc(e["eligibility"])}\n\n'
            f'<b>🏅 Результаты</b>\n{esc(e["result_date"] or "дата не опубликована")}\n\n'
            f'🎖️ <b>Баллы портфолио:</b> победитель {points["победитель"]}, '
            f'призёр {points["призёр"]}, участник {points["участник"]}.\n\n'
            f'<a href="{esc(e["source_url"])}">Проверить первоисточник</a> · '
            f'<a href="{RSOSH_SOURCE_URL}">Перечень РСОШ</a>')
    rows = [[btn('🤖 Спросить ИИ об этом', f'assistant_event:{eid}')]]
    if ADMIN_IDS:
        rows.insert(0, [btn('🏅 Добавить результат', f'result_begin:{eid}')])
    return await send(uid, text, screen(rows, back=f'open:{eid}'), 'events')

def filter_rows(uid):
    o = state(uid).setdefault('filters', {})
    rows = []
    for i in range(0, len(INTERESTS), 2):
        row = []
        for j in range(i, min(i + 2, len(INTERESTS))):
            icon = '✅ ' if o.get('subject') == j else '▫️ '
            row.append(btn(icon + INTERESTS[j], f'fsub:{j}'))
        rows.append(row)
    for i in range(len(KINDS)):
        icon = '✅ ' if o.get('kind') == i else '▫️ '
        rows.append([btn(icon + KINDS[i].capitalize(), f'fkind:{i}')])
    rows += [[btn(('✅ ' if o.get('due') else '▫️ ') + 'Будущие сроки', 'fdue'),
              btn(('✅ ' if o.get('fit') else '▫️ ') + 'Подходит мне', 'ffit')],
             [btn('🔎 Показать', 'fapply'), btn('Сбросить', 'freset')]]
    return screen(rows, back='favorites' if o.get('scope') == 'favorites' else 'home')

async def filters(uid, scope='search', edit=None):
    o = state(uid).setdefault('filters', {})
    o['scope'] = scope
    title = '⭐ Фильтры избранного' if scope == 'favorites' else '🔎 Поиск мероприятий'
    return await send(uid, f'<b>{title}</b>\n\n🎯 <b>Предмет</b> · 🏆 <b>тип события</b> · ⏰ <b>сроки</b>\n\n'
                                '<i>Отметь нужное и нажми «Показать» 👇</i>',
                           filter_rows(uid), scope if scope in BANNERS else 'search', edit=edit)

async def deadlines(uid):
    lines = []
    for eid in tracked_ids(uid, 'participating'):
        e = get_event(eid)
        if not e:
            continue
        for day, label in [(e['registration_deadline'], 'Регистрация'), (e['result_date'], 'Результаты')]:
            if day and day >= today().isoformat():
                lines.append((day, label, e))
        for stage in e['stages']:
            if stage.get('date') and stage['date'] >= today().isoformat():
                lines.append((stage['date'], stage['name'], e))
    lines.sort(key=lambda item: item[0])
    body = '\n'.join(f'📌 <b>{esc(day)}</b> · {esc(label)}: {esc(e["title"][:70])}'
                     for day, label, e in lines[:15]) or '<i>Пока нет отслеживаемых дат.</i>'
    return await send(uid, f'<b>⏰ Мои сроки</b>\n\n{body}\n\n'
                                '<i>Добавь мероприятие через кнопку «Следить» в его карточке.</i>',
                           screen(back='home'), 'deadlines')

async def portfolio(uid):
    state(uid)['stage'] = None
    rows = achievements_for(uid)
    statuses = {'pending': 'на проверке', 'approved': 'подтверждено',
                'rejected': 'отклонено', 'superseded': 'заменено новым результатом'}
    lines = []
    for row in rows[:15]:
        e = get_event(row['event_id'])
        lines.append(f'• <b>{esc((e or {}).get("title", row["event_id"])[:60])}</b> - '
                     f'{esc(row["result"])} <i>({statuses.get(row["status"], row["status"])}, {row["points"]} б.)</i>')
    top = leaderboard()
    rating = '\n'.join(f'{i}. {esc(r["display_name"] or "Участник")} · {r["points"]} б.'
                       for i, r in enumerate(top[:10], 1)) or '<i>Подтверждённых результатов пока нет.</i>'
    score_lines = '\n'.join(
        f'{label}: {POINTS[level]["победитель"]} / {POINTS[level]["призёр"]} / {POINTS[level]["участник"]}'
        for level, label in ((1, 'I уровень'), (2, 'II уровень'), (3, 'III уровень'),
                             (0, 'Без уровня / не подтверждён')))
    text = (f'<b>🏅 Моё портфолио</b>\n\n' + ('\n'.join(lines) or '<i>Пока нет результатов.</i>') +
            f'\n\n<b>🏆 Рейтинг</b>\n{rating}\n\n'
            f'<b>🎖️ Баллы за подтверждённый результат</b>\n'
            f'Победитель / призёр / участник:\n{score_lines}\n\n'
            '<i>Это баллы внутри ОлимпНавигатора, не льготы при поступлении. '
            'Очки начисляются после проверки результата администратором.</i>')
    return await send(uid, text, screen([[btn('➕ Добавить достижение', 'achievement_pick:0')]], back='home'), 'portfolio')


async def achievement_pick(uid, page=0):
    state(uid)['stage'] = None
    events = sorted(all_events(), key=lambda event: event['title'].casefold())
    last = max(0, (len(events) - 1) // PAGE)
    page = max(0, min(page, last))
    rows = [[btn(event['title'][:55], f'result_begin:{event["id"]}')]
            for event in events[page * PAGE:(page + 1) * PAGE]]
    if last:
        rows.append([btn('←', f'achievement_pick:{page-1}'),
                     btn(f'{page+1}/{last+1}', 'noop'),
                     btn('→', f'achievement_pick:{page+1}')])
    return await send(uid, f'<b>➕ Добавить достижение</b>\n\n'
                           f'Выбери мероприятие из каталога: <b>{len(events)}</b> 🏆',
                      screen(rows, back='portfolio'), 'portfolio')

async def assistant(uid, eid=None):
    st = state(uid)
    st['stage'] = 'assistant'
    st['ai_event'] = eid
    st['history'] = []
    save_ai_session(uid, eid)
    remaining = AI_DAILY_LIMIT - ai_messages_today(uid, today().isoformat())
    return await send(uid, '<b>🤖 Диалог с GigaChat</b>\n\nПиши вопросы обычными сообщениями. '
                                'Я ищу мероприятие во всём каталоге и даю ссылку на официальный сайт. '
                                'Диалог можно продолжать сколько угодно дней, но не больше 50 вопросов за день. '
                                f'Сегодня осталось: <b>{remaining}</b>.\n\n'
                                'Когда закончишь, нажми «Завершить диалог» 👇',
                           [[btn('❌ Завершить диалог', 'assistant_finish')]], 'assistant')

async def notify_admins():
    for admin in ADMIN_IDS:
        for row in pending_reviews_for(admin):
            e = get_event(row['event_id'])
            with suppress(MaxAPIError, aiohttp.ClientError, asyncio.TimeoutError):
                dokazatelstvo = f'\n📎 Подтверждение: {esc(row["file_name"] or "без файла")}'
                extra = ([{'type': row['file_kind'], 'payload': {'token': row['file_id']}}]
                         if row['file_id'] and row['file_kind'] in ('image', 'file') else None)
                applicant = get_user(row['tg_id']) or {}
                await send(admin, f'<b>🏅 Проверка #{row["id"]}</b>\n\n'
                                       f'Пользователь: {esc(applicant.get("display_name") or row["tg_id"])}\n'
                                       f'Событие: {esc((e or {}).get("title", row["event_id"]))}\n'
                                       f'Результат: {esc(row["result"])}{dokazatelstvo}',
                                screen([[btn('✅ Подтвердить', f'review:yes:{row["id"]}'),
                                         btn('❌ Отклонить', f'review:no:{row["id"]}')]]), extra=extra)
                mark_admin_delivered(row['id'], admin)

async def save_result(uid, file_token=None, file_kind=None, file_name=None):
    st = state(uid)
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
    await send(uid, f'<b>✅ Заявка на «{esc(event.get("title", eid))}» отправлена на проверку.</b>\n\n'
                         '<i>После подтверждения баллы появятся в рейтинге.</i>', screen(back='portfolio'))
    await notify_admins()

async def callback(uid, action, mid=None):
    target = None if action in ('home', 'assistant_finish') or not mid else (uid, mid)
    token = EDIT_TARGET.set(target)
    try:
        return await callback_action(uid, action, mid)
    finally:
        EDIT_TARGET.reset(token)


async def callback_action(uid, action, mid=None):
    st = state(uid)
    if st.get('stage') == 'assistant' and action not in ('assistant_finish', 'noop') and not (uid in ADMIN_IDS and action.startswith('review:')):
        return await send(uid, '🤖 Сначала заверши диалог с GigaChat 👇',
                          [[btn('❌ Завершить диалог', 'assistant_finish')]])
    if action.startswith('achievement_pick:'):
        return await achievement_pick(uid, int(action.partition(':')[2]))
    if action == 'assistant_finish':
        end_ai_session(uid)
        st['stage'] = None
        st.pop('history', None)
        st.pop('ai_event', None)
        return await home(uid)
    sections = [item[1] for item in MENU]
    card_navigation = action.startswith(('page:', 'open:', 'more:', 'toggle:'))
    if action in sections or action in ('backlist', 'filters') or card_navigation:
        st['stage'] = None
    if action == 'noop':
        return
    if action == 'home':
        return await home(uid)
    if action == 'events':
        return await listing(uid, event_list(uid, 'events'))
    if action == 'favorites':
        return await listing(uid, event_list(uid, 'favorites'), 'favorites')
    if action == 'search':
        st['filters'] = {'scope': 'search'}
        return await filters(uid)
    if action == 'filters':
        return await filters(uid, st.get('list_kind', 'search') if st.get('list_kind') == 'favorites' else 'search')
    if action == 'deadlines':
        return await deadlines(uid)
    if action == 'profile':
        st['stage'] = None
        return await profile(uid)
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
        return await listing(uid, event_list(uid, 'events'))
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
            i = int(raw)
            limit = len(INTERESTS) if key == 'subject' else len(KINDS)
            if not 0 <= i < limit:
                return
            o[key] = None if o.get(key) == i else i
        return await filters(uid, st['filters'].get('scope', 'search'), mid)
    if action == 'fapply':
        o = st.get('filters', {})
        scope = o.get('scope', 'search')
        events = event_list(uid, 'favorites') if scope == 'favorites' else all_events()
        return await listing(uid, filter_events(events, o, get_user(uid)), scope)
    if action == 'notifications':
        user = get_user(uid)
        update_user(uid, notifications=not user['notifications'])
        return await profile(uid)
    if action == 'support':
        rows = [[{'type': 'clipboard', 'text': '📋 Скопировать номер',
                  'payload': '+79224899599'}]]
        return await send(uid, '💬 <b>Поддержка</b>\n\n+7 922 489-95-99',
                               screen(rows, back='profile'))
    if action == 'school':
        st['stage'] = 'school'
        return await send(uid, '<b>🏫 Школа</b>\n\nНапиши название школы одним сообщением.',
                               screen(back='profile'))
    if action == 'delete_prompt':
        return await send(uid, '<b>🗑 Удалить профиль?</b>\n\nЭто удалит избранное, сроки и достижения.',
                               screen([[btn('Да, удалить', 'delete_yes')]], back='profile'))
    if action == 'delete_yes':
        from core import connect
        with connect() as db:
            db.execute('DELETE FROM sent_notifications WHERE tg_id=?', (uid,))
            db.execute('DELETE FROM users WHERE tg_id=?', (uid,))
        st.clear()
        return await send(uid, '✅ Данные удалены.', screen(back='home'))
    if action.startswith('result_begin:'):
        eid = action.partition(':')[2]
        if not get_event(eid):
            return await portfolio(uid)
        if not ADMIN_IDS:
            return await more(uid, eid)
        st['result_event'] = eid
        st['stage'] = None
        return await send(uid, '<b>🏅 Добавить результат</b>\n\nКакой у тебя результат?',
                               screen([[btn('Участник', 'result:участник'), btn('Призёр', 'result:призёр')],
                                       [btn('Победитель', 'result:победитель')]], back='portfolio'))
    if action.startswith('result:'):
        result = action.partition(':')[2]
        if result not in ('участник', 'призёр', 'победитель') or not st.get('result_event'):
            return
        st['result'] = result
        st['stage'] = 'result_file'
        return await send(uid, '<b>📎 Подтверждение результата</b>\n\n'
                                    'Пришли диплом или сертификат файлом/фото. '
                                    'Если его нет, нажми «Без файла».',
                               screen([[btn('Без файла', 'result_skip')]], back='portfolio'))
    if action == 'result_skip':
        return await save_result(uid)
    if action.startswith('review:') and uid in ADMIN_IDS:
        _, decision, raw = action.split(':')
        row = review_achievement(int(raw), uid, decision == 'yes')
        if row:
            event = get_event(row['event_id']) or {}
            title = esc(event.get('title', row['event_id']))
            await send(row['tg_id'],
                            f'✅ Заявка на «{title}» одобрена!\n\n'
                            f'🏅 Результат: <b>{esc(row["result"])}</b>\n'
                            f'🎯 Начислено <b>{row["points"]}</b> баллов. Место в рейтинге обновлено.'
                            if decision == 'yes' else
                            f'❌ Заявка на «{title}» отклонена. Проверь подтверждение и отправь снова.',
                            screen(back='portfolio'))
            return await send(uid, '✅ Проверка выполнена.', screen(back='home'))
    return await send(uid, '🤔 Я ещё не знаю такой команды. Вернись на главную 👇',
                      screen(back='home'))

async def message(uid, message):
    body = message.get('body') or {}
    text = (body.get('text') or '').strip()
    attachments = body.get('attachments') or []
    st = state(uid)
    if st.get('stage') == 'assistant':
        finish_button = [[btn('❌ Завершить диалог', 'assistant_finish')]]
        if text == '/myid':
            return await send(uid, f'🪪 Твой MAX ID: <code>{uid}</code>', finish_button)
        if text in ('/start', 'старт', 'Старт') or not text:
            return await send(uid, '🤖 Диалог ещё открыт. Напиши вопрос или заверши его 👇',
                              finish_button)
        day = today().isoformat()
        if not use_ai_message(uid, day, AI_DAILY_LIMIT):
            return await send(uid, '⏳ На сегодня закончились 50 вопросов к GigaChat. '
                                  'Завтра можно продолжить этот же диалог.\n\n'
                                  'Если хочешь вернуться в меню, заверши диалог 👇',
                              finish_button)
        user = get_user(uid)
        cards = ai_cards(text, st.get('history'), user, st.get('ai_event'))
        answer, provider = await ai_answer(text, cards, st.get('history'), today().isoformat())
        st.setdefault('history', []).extend([{'role': 'user', 'text': text},
                                              {'role': 'assistant', 'text': answer}])
        st['history'] = st['history'][-20:]
        save_ai_session(uid, st.get('ai_event'), st['history'])
        title = '🤖 Отвечает GigaChat' if provider == 'GigaChat' else '📚 Подборка из каталога'
        remaining = AI_DAILY_LIMIT - ai_messages_today(uid, day)
        return await send(uid, f'<b>{title}</b>\n\n{ai_text(answer)}\n\n'
                               f'<i>Осталось вопросов сегодня: {remaining} из {AI_DAILY_LIMIT}.</i>',
                               finish_button)
    if text in ('/start', 'старт', 'Старт'):
        return await home(uid)
    if text == '/myid':
        return await send(uid, f'🪪 Твой MAX ID: <code>{uid}</code>', screen(back='profile'))
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
        return await send(uid, f'✅ Импортировано: новых {added}, обновлено {updated}.', screen(back='home'))
    if st.get('stage') == 'school' and text:
        update_user(uid, school=text[:150])
        st['stage'] = None
        return await profile(uid)
    if st.get('stage') == 'result_file':
        for item in attachments:
            kind = item.get('type')
            if kind in ('image', 'file'):
                payload = item.get('payload') or {}
                token = payload.get('token')
                if token:
                    return await save_result(uid, token, kind,
                                                  payload.get('filename') or 'Подтверждение')
        return await send(uid, '📎 Пришли фото или файл. Можно выбрать «Без файла».',
                               screen([[btn('Без файла', 'result_skip')]], back='portfolio'))
    if text.startswith('/import') and uid in ADMIN_IDS:
        return await send(uid, '📦 Пришли JSON-файл с подписью /import.',
                               screen(back='home'))
    return await send(uid, '🤔 Я ещё не знаю такой команды. Выбери раздел на главной 👇',
                      screen(back='home'))

async def handle_update(event):
    typ = event.get('update_type')
    if typ == 'bot_started':
        uid = (event.get('user') or {}).get('user_id')
        if uid:
            ensure_user(uid)
            remember_name(uid, event.get('user') or {})
            if state(uid).get('stage') == 'assistant':
                await send(uid, '🤖 Диалог с GigaChat ещё открыт. Напиши вопрос или заверши его 👇',
                           [[btn('❌ Завершить диалог', 'assistant_finish')]])
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

async def scheduler():
    posledniy_sbor = None
    while True:
        with suppress(Exception):
            now = datetime.now(ZoneInfo(TIMEZONE))
            if now.hour >= 9:
                for uid, e, key, label in due_notifications(now.date()):
                    with suppress(MaxAPIError, aiohttp.ClientError, asyncio.TimeoutError):
                        await send(uid, f'⏰ {esc(label)}: {esc(e["title"])}',
                                        screen([[btn('Открыть', f'open:{e["id"]}')]]))
                        mark_sent(uid, e['id'], key)
            if ADMIN_IDS:
                await notify_admins()
            if now.hour >= 6 and posledniy_sbor != now.date():
                events = await asyncio.to_thread(collect_all, now.date())
                if events:
                    _, _, changes = import_events(events)
                    for eid, label in changes:
                        e = get_event(eid)
                        if e:
                            for uid in subscribers_for(eid):
                                with suppress(MaxAPIError, aiohttp.ClientError, asyncio.TimeoutError):
                                    await send(uid, f'🔔 Обновлено: {esc(label)} у «{esc(e["title"])}».',
                                                    screen([[btn('Открыть', f'open:{eid}')]]))
                    posledniy_sbor = now.date()
        await asyncio.sleep(900)


async def main():
    global API
    if not MAX_BOT_TOKEN:
        raise SystemExit
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    init_db()
    if not all_events():
        import_events(json.loads((ROOT / 'catalog.seed.json').read_text(encoding='utf-8')))
    timeout = aiohttp.ClientTimeout(total=45)
    connector = aiohttp.TCPConnector(ssl=russian_ssl())
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        api = MaxAPI(MAX_BOT_TOKEN, session)
        API = api
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(scheduler())
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
                    await handle_update(event)
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
                        LOG.info('Webhook is active')
                        await asyncio.Event().wait()
            marker = None
            while True:
                data = None
                with suppress(MaxAPIError, aiohttp.ClientError, asyncio.TimeoutError):
                    data = await api.updates(marker)
                if data is None:
                    await asyncio.sleep(3)
                    continue
                for event in data.get('updates', []):
                    with suppress(Exception):
                            await handle_update(event)
                marker = data.get('marker', marker)


if __name__ == '__main__':
    asyncio.run(main())
