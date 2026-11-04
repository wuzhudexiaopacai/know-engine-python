# KnowEngine-Python 备份脚本（Windows / Docker Compose 部署）
#
# 备份内容：
#   1. MySQL 全量逻辑备份（mysqldump --single-transaction，不锁库）
#   2. MinIO 数据卷离线打包（文档原件/产物）
#   3. Redis RDB 快照（可选，-IncludeRedis；验证码/会话缓冲可丢，Stream 积压事件会随快照恢复）
#
# ES 向量索引与 Neo4j 图谱可由 MySQL 中的分段数据重建（重新激活文档或等待补偿任务），不纳入备份。
#
# 用法：  .\scripts\backup.ps1 [-OutDir .\backups] [-IncludeRedis]
param(
    [string]$OutDir = ".\backups",
    [switch]$IncludeRedis
)
$ErrorActionPreference = "Stop"

$ts = Get-Date -Format "yyyyMMdd-HHmmss"
New-Item -ItemType Directory -Force $OutDir | Out-Null
$abs = (Resolve-Path $OutDir).Path

Write-Host "[1/3] MySQL 全量导出 ..."
# 在容器内生成再拷出：避免 PowerShell 管道改写换行/编码
docker exec ke-mysql sh -c 'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines --triggers know_engine > /tmp/ke-backup.sql'
docker cp ke-mysql:/tmp/ke-backup.sql "$abs\mysql-$ts.sql"
docker exec ke-mysql rm -f /tmp/ke-backup.sql

Write-Host "[2/3] MinIO 数据卷打包 ..."
docker run --rm -v know-engine-python_minio-data:/data:ro -v "${abs}:/backup" alpine `
    tar czf "/backup/minio-data-$ts.tar.gz" -C /data .

if ($IncludeRedis) {
    Write-Host "[3/3] Redis RDB 快照 ..."
    docker exec ke-redis redis-cli BGSAVE | Out-Null
    Start-Sleep -Seconds 3
    docker run --rm -v know-engine-python_redis-data:/data:ro -v "${abs}:/backup" alpine `
        tar czf "/backup/redis-data-$ts.tar.gz" -C /data .
}

Write-Host "备份完成： $abs"
Get-ChildItem $abs -Filter "*$ts*" | ForEach-Object { "  $($_.Name)  $([math]::Round($_.Length/1MB,2)) MB" }
