FROM public.ecr.aws/lambda/python:3.12

# Add archive tools needed to unpack the static ffmpeg release.
RUN microdnf install -y tar xz && microdnf clean all

# Install ffmpeg for WAV -> MP3 conversion.
RUN curl -L -o /tmp/ffmpeg.tar.xz https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz \
	&& mkdir -p /opt/ffmpeg \
	&& tar -xJf /tmp/ffmpeg.tar.xz -C /opt/ffmpeg --strip-components=1 \
	&& cp /opt/ffmpeg/ffmpeg /usr/local/bin/ffmpeg \
	&& chmod +x /usr/local/bin/ffmpeg \
	&& rm -rf /tmp/ffmpeg.tar.xz /opt/ffmpeg

# Install dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy model + app
COPY app.py ${LAMBDA_TASK_ROOT}
COPY en_US-ljspeech-high.onnx ${LAMBDA_TASK_ROOT}
COPY en_US-ljspeech-high.onnx.json ${LAMBDA_TASK_ROOT}

CMD ["app.lambda_handler"]