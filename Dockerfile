# ✅ 改成从你自己的 ACR 拉基础镜像
FROM crpi-v2fmzydhnzmlpzjc.cn-shanghai.personal.cr.aliyuncs.com/machenkai/python:3.10-slim

# 设置工作目录
WORKDIR /app

# 拷贝代码到容器
COPY . /app

# 安装依赖（使用阿里云镜像源加速）
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    && rm -rf /root/.cache/pip


# 暴露端口
EXPOSE 12081

# 启动命令
CMD ["python", "app.py"]