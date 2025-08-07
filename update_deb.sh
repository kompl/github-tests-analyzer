#!/bin/bash

set +x

# Константы
PARENT_DIR="/Users/nikolaevigor/dev"  # Замените на реальный путь к родительской директории
SSH_KEY_PATH="/Users/nikolaevigor/.ssh/id_rsa"

# Список проектов и их директорий
PROJECTS_LIST=("hydra-migration" )
# PROJECTS_LIST=("hard" "hpd" "hmed" "hamd" "hcd" "hcr")
get_name() {
    case "$1" in
        "hydra-migration") echo "hydra-migration" ;;
        *) echo "$1" ;;
    esac
}

get_dir() {
    case "$1" in
        "hydra-migration") echo "hydra-migration" ;;
        *) echo "$1" ;;
    esac
}

get_user() {
    case "$1" in
        "hydra-migration") echo "hydra-migration" ;;
        *) echo "$1" ;;
    esac
}

get_lock_file() {
    case "$1" in
        "hcr") echo "Gemfile.lock" ;;
        "hydra-migration") echo "frontend/yarn.lock" ;;
        *) echo "poetry.lock" ;;
    esac
}

# Массивы для отслеживания результатов
SUCCESS_PROJECTS=()
FAILED_PROJECTS=()

echo "=== Начало сборки проектов ==="
echo ""

for i in "${!PROJECTS_LIST[@]}"; do
    PROJECT_KEY="${PROJECTS_LIST[$i]}"
    PROJECT_NAME=$(get_name "$PROJECT_KEY")
    PROJECT_DIR=$(get_dir "$PROJECT_KEY")
    PROJECT_USER=$(get_user "$PROJECT_KEY")
    FULL_PATH="$PARENT_DIR/$PROJECT_DIR"
    LOCK_FILE=$(get_lock_file "$PROJECT_KEY")

    echo "🔄 Обработка проекта: $PROJECT_NAME (директория: $PROJECT_DIR)"
    echo "📁 Путь: $FULL_PATH"

    # Проверяем существование директории
    if [ ! -d "$FULL_PATH" ]; then
        echo "❌ Директория $FULL_PATH не найдена"
        FAILED_PROJECTS+=("$PROJECT_NAME: директория не найдена")
        echo ""
        continue
    fi

    # Переходим в директорию проекта
    cd "$FULL_PATH" || {
        echo "❌ Не удалось перейти в директорию $FULL_PATH"
        FAILED_PROJECTS+=("$PROJECT_NAME: не удалось перейти в директорию")
        echo ""
        continue
    }

    echo "🔨 Обновление проекта $PROJECT_NAME..."

    # Сборка Docker образа
    if git stash && git co master && git pull && git sta; then
        echo "✅ Проект $PROJECT_NAME обновлен"
    else
        echo "❌ Ошибка при обновлении проекта $PROJECT_NAME"
        FAILED_PROJECTS+=("$PROJECT_NAME: ошибка при обновлении проекта")
        echo ""
        continue
    fi

    echo "🔨 Сборка Docker образа для $PROJECT_NAME..."

    # Сборка Docker образа
    if docker buildx build \
        --platform linux/amd64 \
        --build-arg SSH_PRIVATE_KEY="$(cat "$SSH_KEY_PATH")" \
        -t "$PROJECT_NAME" .; then
        echo "✅ Docker образ $PROJECT_NAME успешно собран"
    else
        echo "❌ Ошибка при сборке Docker образа для $PROJECT_NAME"
        FAILED_PROJECTS+=("$PROJECT_NAME: ошибка сборки Docker образа")
        echo ""
        continue
    fi

    echo "📦 Копирование $LOCK_FILE из контейнера..."

    # Создание временного контейнера для копирования файла
    CONTAINER_ID=$(docker create --platform linux/amd64 "$PROJECT_NAME")

    if [ $? -eq 0 ]; then
        # Копирование $LOCK_FILE из контейнера в текущую директорию
        if docker cp "$CONTAINER_ID:/opt/hydra/$PROJECT_NAME/$LOCK_FILE" "$PWD/$LOCK_FILE"; then
            echo "✅ $LOCK_FILE успешно скопирован для $PROJECT_NAME"
            SUCCESS_PROJECTS+=("$PROJECT_NAME")
        else
            echo "❌ Ошибка при копировании $LOCK_FILE для $PROJECT_NAME"
            FAILED_PROJECTS+=("$PROJECT_NAME: ошибка копирования $LOCK_FILE")
        fi

        # Удаление временного контейнера
        docker rm "$CONTAINER_ID" > /dev/null 2>&1
    else
        echo "❌ Ошибка при создании временного контейнера для $PROJECT_NAME"
        FAILED_PROJECTS+=("$PROJECT_NAME: ошибка создания временного контейнера")
    fi

    echo ""
done

# Вывод итогового отчета
echo "=== ИТОГОВЫЙ ОТЧЕТ ==="
echo ""

if [ ${#SUCCESS_PROJECTS[@]} -gt 0 ]; then
    echo "✅ Успешно обработанные проекты (${#SUCCESS_PROJECTS[@]}):"
    for project in "${SUCCESS_PROJECTS[@]}"; do
        echo "  - $project"
    done
    echo ""
fi

if [ ${#FAILED_PROJECTS[@]} -gt 0 ]; then
    echo "❌ Проекты с ошибками (${#FAILED_PROJECTS[@]}):"
    for project in "${FAILED_PROJECTS[@]}"; do
        echo "  - $project"
    done
    echo ""
fi

echo "=== Завершено ==="
