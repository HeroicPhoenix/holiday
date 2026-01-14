# ✅ 改成从你自己的 ACR 拉基础镜像
FROM crpi-v2fmzydhnzmlpzjc.cn-shanghai.personal.cr.aliyuncs.com/machenkai/python:3.10-slim

# 设置工作目录
WORKDIR /app

COPY requirements.txt /app/requirements.txt

# 安装依赖（使用阿里云镜像源加速）
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    && rm -rf /root/.cache/pip

# 拷贝代码到容器
COPY app /app

# 数据卷（让 /data 可挂载）
VOLUME ["/data"]

# 暴露端口
EXPOSE 12081

# 用 uvicorn 前台启动（与 compose 里一致）
CMD ["uvicorn","main:app","--host","0.0.0.0","--port","12081"]