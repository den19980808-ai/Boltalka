#!/data/data/com.termux/files/usr/bin/bash

echo "📦 Создание рабочей директории..."
mkdir -p ~/workdir
chmod 755 ~/workdir

echo "🔧 Настройка SSHD (разрешение переменных окружения)..."
CONFIG="/data/data/com.termux/files/usr/etc/ssh/sshd_config"

if ! grep -q "PermitUserEnvironment yes" $CONFIG; then
    echo "PermitUserEnvironment yes" >> $CONFIG
else
    echo "PermitUserEnvironment уже включён"
fi

echo "📝 Создание файла окружения..."
mkdir -p ~/.ssh
cat > ~/.ssh/environment <<EOF
HOME=/data/data/com.termux/files/home/workdir
EOF

chmod 600 ~/.ssh/environment

echo "🔁 Перезапуск sshd..."
sv restart sshd || {
    echo "Ошибка перезапуска через sv, пробую альтернативу..."
    pkill sshd
    sshd
}

echo "✅ Готово! VS Code теперь сможет подключаться!"
echo "📁 Рабочая папка: ~/workdir"
echo "👉 Используй её как корень проекта"
