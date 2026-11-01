"""KnowEngine Python —— 企业级智能知识引擎（FastAPI 实现）。

包结构：
    config        全局配置（pydantic-settings）
    core          横切能力：日志、异常、依赖注入
    infra         基础设施：雪花ID、缓存、锁、熔断、JSON 修复、文件存储、向量检索
    models        ORM 实体与建表脚本
    services      外部服务封装：LLM / Embedding / Rerank / 文档解析
    modules       业务模块（controller=路由, service=业务逻辑, schemas=数据模型）
        auth          认证与会话令牌
        document      知识文档：分片上传、转换、切片、向量化
        chat          对话：SSE 流式、会话与消息管理
        rag           RAG 管道：意图识别、改写、路由、检索、聚合、生成
        qa_cache      审核式问答缓存
    jobs          定时补偿任务
"""

__version__ = "1.0.0"
