#!/bin/bash

set +x

# Константы
PARENT_DIR="/Users/nikolaevigor/dev"  # Замените на реальный путь к родительской директории
SSH_KEY_PATH="/Users/nikolaevigor/.ssh/id_rsa"

# Список проектов и их директорий
PROJECTS_LIST=("hccp" )
# PROJECTS_LIST=("hard" "hpd" "hmed" "hamd" "hcd" "hcr" "hydra-migration" "hocs" "release-helper" "compatibility-table-updater" "hccp")
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

get_workdir() {
    case "$1" in
        "release-helper") echo "/app" ;;
        "compatibility-table-updater") echo "/app" ;;
        *) echo "/opt/hydra/$1" ;;
    esac
}

get_lock_files() {
    case "$1" in
        "hcr") echo "Gemfile.lock" ;;
        "release-helper") echo "Gemfile.lock" ;;
        "compatibility-table-updater") echo "Gemfile.lock" ;;
        "hydra-migration") echo "frontend/yarn.lock Gemfile.lock" ;;
        "hccp") echo "frontend/yarn.lock Gemfile.lock" ;;
        "hydra-messages-relay") echo "go.mod go.sum" ;;
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
    LOCK_FILES_STR=$(get_lock_files "$PROJECT_KEY")
    WORKDIR=$(get_workdir "$PROJECT_KEY")

    # Преобразуем строку в массив
    read -ra LOCK_FILES <<< "$LOCK_FILES_STR"

    echo "🔄 Обработка проекта: $PROJECT_NAME (директория: $PROJECT_DIR)"
    echo "📁 Путь: $FULL_PATH"
    echo "📋 Lock файлы: ${LOCK_FILES[*]}"

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

    # Создание временного контейнера для копирования файлов
    CONTAINER_ID=$(docker create --platform linux/amd64 "$PROJECT_NAME")

    if [ $? -eq 0 ]; then
        COPY_ERRORS=()
        COPY_SUCCESS=()

        # Копирование каждого lock файла из контейнера
        for LOCK_FILE in "${LOCK_FILES[@]}"; do
            echo "📦 Копирование $LOCK_FILE из контейнера..."

            if docker cp "$CONTAINER_ID:$WORKDIR/$LOCK_FILE" "$PWD/$LOCK_FILE"; then
                echo "✅ $LOCK_FILE успешно скопирован для $PROJECT_NAME"
                COPY_SUCCESS+=("$LOCK_FILE")
            else
                echo "❌ Ошибка при копировании $LOCK_FILE для $PROJECT_NAME"
                COPY_ERRORS+=("$LOCK_FILE")
            fi
        done

        # Удаление временного контейнера
        docker rm "$CONTAINER_ID" > /dev/null 2>&1

        # Проверяем результаты копирования
        if [ ${#COPY_ERRORS[@]} -eq 0 ]; then
            echo "✅ Все lock файлы успешно скопированы для $PROJECT_NAME"
            SUCCESS_PROJECTS+=("$PROJECT_NAME")
        else
            echo "❌ Ошибки при копировании файлов для $PROJECT_NAME: ${COPY_ERRORS[*]}"
            FAILED_PROJECTS+=("$PROJECT_NAME: ошибка копирования файлов: ${COPY_ERRORS[*]}")
        fi
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
