-- docker-entrypoint 首次启动自动执行：建库 + 应用账号（最小权限，不启用根账号远程访问）
CREATE DATABASE IF NOT EXISTS know_engine DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'know_engine'@'%' IDENTIFIED BY 'know_engine';
GRANT ALL PRIVILEGES ON know_engine.* TO 'know_engine'@'%';
FLUSH PRIVILEGES;
