# SlideForge Design Server

Онлайн-конвертер тем для Android-приложения SlideForge. Сервер принимает `.pptx`/`.potx`, удаляет редактируемый демонстрационный текст, рендерит каждый слайд целиком и возвращает ZIP-пакет с точными фоновыми макетами.

## API

- `GET /health` — проверка сервера.
- `POST /convert` — multipart-загрузка, поле `file`; необязательное поле `name`.
- Необязательная защита: задайте `CONVERTER_TOKEN`, затем передавайте его в `X-SlideForge-Token`.

Пример:

```bash
curl -f -X POST \
  -F "file=@template.pptx" \
  -F "name=Мой дизайн" \
  https://YOUR-SERVICE.onrender.com/convert \
  -o theme.slideforge-theme.zip
```

## Развёртывание на Render

1. Загрузите содержимое этой папки в репозиторий GitHub.
2. В Render выберите **New → Blueprint**.
3. Подключите репозиторий `slideforge-design-server`.
4. Render прочитает `render.yaml` и соберёт Docker-контейнер.
5. Проверьте адрес `https://...onrender.com/health`.

При желании добавьте в Environment секрет `CONVERTER_TOKEN`. Не добавляйте токен в GitHub.

## Ограничения

Сервер сохраняет весь визуальный макет, изображения, фигуры и фон презентации. Весь редактируемый текст на слайдах очищается. Текст, уже встроенный внутрь картинки, технически является частью изображения и не удаляется.
