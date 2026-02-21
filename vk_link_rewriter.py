import os
import time
import re
import sys
from urllib.parse import urlparse
from typing import Optional
from collections import deque

import vk_api
from vk_api.exceptions import ApiError
from vk_api.vk_api import DEFAULT_USERAGENT
from dotenv import load_dotenv
import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    Retry = None

# Загружаем переменные окружения из файла .env (если есть)
load_dotenv()

# Конфигурация
VK_TOKEN: Optional[str] = os.getenv("VK_TOKEN")

vk_session: Optional[vk_api.VkApi] = None
vk = None

request_times = deque()

def _build_http_session(
    timeout: tuple[float, float] = (10.0, 60.0),
    retries: int = 3,
    backoff_factor: float = 0.5,
) -> requests.Session:
    session = requests.Session()
    session.headers.setdefault("User-agent", DEFAULT_USERAGENT)

    if Retry is not None:
        retry = Retry(
            total=retries,
            connect=retries,
            read=retries,
            status=retries,
            backoff_factor=backoff_factor,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

    original_request = session.request

    def request_with_timeout(method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = timeout
        return original_request(method, url, **kwargs)

    session.request = request_with_timeout  # type: ignore[assignment]
    return session


def init_vk_api(token: Optional[str] = None, ignore_env_token: bool = False) -> None:
    """
    Инициализирует VK API по токену.
    Если токен не передан и ignore_env_token=False, используется VK_TOKEN из .env.
    """
    global VK_TOKEN, vk_session, vk

    token_arg = (token or "").strip() or None
    http_session = _build_http_session()

    if token_arg:
        VK_TOKEN = token_arg
    elif not ignore_env_token and VK_TOKEN:
        pass
    else:
        raise RuntimeError("Укажите VK токен (или задайте VK_TOKEN в .env).")

    vk_session = vk_api.VkApi(token=VK_TOKEN, session=http_session)
    vk = vk_session.get_api()


# Вспомогательная функция для безопасного ожидания при превышении лимитов
def safe_request(method, **kwargs):
    """Выполняет запрос к API, автоматически повторяет при ошибке 6 (слишком много запросов)."""
    if vk_session is None:
        raise RuntimeError("VK API не инициализирован. Вызовите init_vk_api().")

    global request_times
    delay = 0.34  # начальная задержка ~3 запроса в секунду
    net_delay = 1.0
    while True:
        current_time = time.time()
        # Удаляем старые запросы старше 60 секунд
        while request_times and request_times[0] < current_time - 60:
            request_times.popleft()

        if len(request_times) >= 180:
            # Ждём, пока самый старый запрос выйдет за пределы окна
            sleep_time = (request_times[0] - (current_time - 60)) + 0.01  # небольшой буфер
            print(f"⚠️  Достигнут лимит 180 запросов/мин, пауза {sleep_time:.2f} сек...")
            time.sleep(sleep_time)
            current_time = time.time()
            # Повторно удаляем старые после сна
            while request_times and request_times[0] < current_time - 60:
                request_times.popleft()

        # Добавляем текущий запрос
        request_times.append(current_time)

        try:
            # Правильный вызов через vk_session.method
            return vk_session.method(method, kwargs)
        except ApiError as e:
            if e.code == 6:  # Too many requests per second
                print(f"⚠️  Превышение лимита запросов, пауза {delay:.2f} сек...")
                time.sleep(delay)
                delay *= 2  # экспоненциальное увеличение паузы
                if delay > 10:
                    delay = 10
            else:
                raise e
        except requests.exceptions.RequestException as e:
            print(f"⚠️  Сетевая ошибка при вызове {method}: {e}. Повтор через {net_delay:.1f} сек...")
            time.sleep(net_delay)
            net_delay = min(net_delay * 2, 20.0)

def resolve_owner_id(screen_name):
    """Преобразует короткое имя или ссылку сообщества в отрицательный owner_id."""
    # Извлекаем screen_name из разных форматов
    # Примеры: https://vk.com/public123, vk.com/club_name, club123, public123, id123
    original = screen_name.strip()
    
    # Обработка ссылок без протокола: vk.com/club123 или vk.ru/club123
    if '/' in original and ('vk.com' in original or 'vk.ru' in original):
        # Извлекаем часть после последнего слеша
        parts = original.split('/')
        screen_name = parts[-1] if parts else original
    else:
        parsed = urlparse(original)
        if parsed.netloc:
            # это ссылка вида https://vk.com/durov
            path = parsed.path.strip('/')
            screen_name = path
        else:
            screen_name = original
    
    # Проверка на числовые группы: club123456, public123456, event123456
    if screen_name.startswith(('club', 'public', 'event')):
        match = re.search(r'\d+', screen_name)
        if match:
            return -int(match.group())
    
    # Проверка на id пользователя
    if screen_name.startswith('id'):
        # страница пользователя, нас не интересует, но вернём положительное число
        return int(screen_name[2:])
    
    # Иначе считаем, что это просто screen_name и пытаемся разрешить через API
    try:
        # Пробуем преобразовать screen_name через utils.resolveScreenName
        print(f"Разрешаем screen_name: {screen_name}")
        result = safe_request('utils.resolveScreenName', screen_name=screen_name)
        if result and result.get('type') in ('group', 'page', 'event'):
            return -result['object_id']
        else:
            print(f"⚠️  Не удалось определить ID для {screen_name} (тип: {result.get('type') if result else 'нет результата'})")
            return None
    except ApiError as e:
        print(f"⚠️  Ошибка при разрешении имени {screen_name}: {e}")
        return None

def replace_in_text(text, old, new):
    """Заменяет old на new в тексте, избегая случайной замены частей URL."""
    # Простая замена строки, но можно усложнить регуляркой, если нужно точное совпадение
    return text.replace(old, new)

def edit_post(owner_id, post_id, new_text, attachments=None):
    """Редактирует пост, сохраняя вложения."""
    params = {
        'owner_id': owner_id,
        'post_id': post_id,
        'message': new_text,
        'from_group': 1  # обязательно, если используем токен пользователя
    }
    print(params)
    if attachments:
        # attachments приходит как список словарей, преобразуем в строку для API
        attach_str = ','.join([f"{a['type']}{a[a['type']]['owner_id']}_{a[a['type']]['id']}" for a in attachments])
        params['attachments'] = attach_str
    try:
        safe_request('wall.edit', **params)
        return True
    except ApiError as e:
        print(f"    ❌ Ошибка редактирования поста {post_id}: {e}")
        return False

def edit_comment(owner_id, comment_id, new_text, attachments=None):
    """Редактирует комментарий, сохраняя вложения."""
    params = {
        'owner_id': owner_id,
        'comment_id': comment_id,
        'message': new_text
    }
    if attachments:
        attach_str = ','.join([f"{a['type']}{a[a['type']]['owner_id']}_{a[a['type']]['id']}" for a in attachments])
        params['attachments'] = attach_str
    try:
        safe_request('wall.editComment', **params)
        return True
    except ApiError as e:
        print(f"    ❌ Ошибка редактирования комментария {comment_id}: {e}")
        return False

def process_community(community_url, old_link, new_link):
    """Обрабатывает одно сообщество: ищет посты и комментарии, заменяет ссылки."""
    owner_id = resolve_owner_id(community_url)
    if owner_id is None:
        print(f"❌ Пропускаем {community_url}: не удалось определить ID")
        return

    print(f"\n📌 Обрабатываем сообщество ID = {owner_id}")

    # 1. Поиск постов, содержащих старую ссылку
    offset = 0
    total_edited_posts = 0
    while True:
        try:
            posts_response = safe_request('wall.get',
                                          owner_id=owner_id,
                                          count=100,
                                          offset=offset,
                                          extended=0)  # Получаем все посты пачками
    
            if posts_response and isinstance(posts_response, dict) and 'items' in posts_response:
                items = posts_response['items']
                if not items:
                    print(f"  ⏺️ В сообществе {owner_id} нет постов (или конец стены).")
                    break
            else:
                print(f"  ⚠️ Неожиданный ответ от wall.get: {posts_response}")
                break
    
        except ApiError as e:
            error_code = getattr(e, 'code', 'неизвестный')
            error_msg = getattr(e, 'message', str(e))
            print(f"❌ Ошибка при получении постов в {owner_id}: код {error_code}, сообщение: {error_msg}")
            if error_code in [15, 30, 100, 1051]:  # Добавьте 1051 для обработки
                print(f"   Сообщество {owner_id} недоступно (возможно, нет прав или тип профиля).")
            break
    
        for post in items:
            post_id = post['id']
            text = post.get('text', '')
            new_text = replace_in_text(text, old_link, new_link)
            if new_text != text:
                attachments = post.get('attachments', [])
                print(f"  ✏️  Редактируем пост {post_id}...")
                if edit_post(owner_id, post_id, new_text, attachments):
                    total_edited_posts += 1
            else:
                print(f"  ⏭️  Пост {post_id} – текст не изменился, пропускаем")

            time.sleep(0.34)
    
            # Комментарии для всех постов
            process_comments_for_post(owner_id, post_id, old_link, new_link)
    
        if len(items) < 100:
            break
        offset += 100
        time.sleep(0.34)
    
    print(f"  ✅ Всего отредактировано постов: {total_edited_posts}")

def process_comment(owner_id, comment, old_link, new_link):
    comment_id = comment['id']
    text = comment.get('text', '')
    if old_link not in text:
        return 0

    new_text = replace_in_text(text, old_link, new_link)
    if new_text == text:
        return 0

    attachments = comment.get('attachments', [])
    print(f"    ✏️  Редактируем комментарий {comment_id}...")
    if edit_comment(owner_id, comment_id, new_text, attachments):
        return 1
    return 0

def process_comments_for_post(owner_id, post_id, old_link, new_link):
    """Находит и редактирует комментарии к посту, содержащие старую ссылку."""
    offset = 0
    total_edited_comments = 0
    while True:
        try:
            comments = safe_request('wall.getComments',
                                    owner_id=owner_id,
                                    post_id=post_id,
                                    count=100,
                                    offset=offset,
                                    need_likes=0,
                                    need_threads=1,
                                    thread_items=10)
        except ApiError as e:
            print(f"    ⚠️  Не удалось получить комментарии к посту {post_id}: {e}")
            break

        items = comments.get('items', [])
        if not items:
            break

        for comment in items:
            total_edited_comments += process_comment(owner_id, comment, old_link, new_link)
            time.sleep(0.34)

            if 'thread' in comment:
                thread_items = comment['thread'].get('items', [])
                for thread_comment in thread_items:
                    total_edited_comments += process_comment(owner_id, thread_comment, old_link, new_link)
                    time.sleep(0.34)

        if len(items) < 100:
            break
        offset += 100
        time.sleep(0.34)

    if total_edited_comments:
        print(f"    ✅ Комментариев отредактировано: {total_edited_comments}")

def main():
    print("🔄 Массовая замена ссылок в постах и комментариях ВК")

    global VK_TOKEN
    if not VK_TOKEN:
        VK_TOKEN = input("Введите VK_TOKEN (или задайте в .env): ").strip() or None

    try:
        init_vk_api()
    except Exception as e:
        print(f"❌ Ошибка инициализации VK API: {e}")
        return

    old_link = input("Введите ссылку, которую нужно заменить: ").strip()
    new_link = input("Введите новую ссылку: ").strip()
    print("Введите ссылки на сообщества (по одной в строке, пустая строка - конец ввода):")
    communities = []
    while True:
        line = input().strip()
        if not line:
            break
        communities.append(line)

    if not communities:
        print("❌ Не указано ни одного сообщества.")
        return

    print(f"\n🔍 Начинаем обработку {len(communities)} сообществ...")
    for comm in communities:
        process_community(comm, old_link, new_link)
        # Пауза между сообществами
        time.sleep(1)

    print("\n🎉 Работа завершена!")

if __name__ == "__main__":
    main()