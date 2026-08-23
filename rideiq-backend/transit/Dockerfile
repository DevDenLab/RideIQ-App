# RideIQ transit engine — OpenTripPlanner behind FastAPI.
#
# We build our own image instead of pulling opentripplanner/opentripplanner so the
# jar here and the jar that built graph.obj are pinned to the same version by one
# ARG. That matters more than it sounds: OTP refuses to load a graph serialized by
# a different version, and the failure arrives at container start, in production.
#
# The graph itself is NOT baked in — it is ~100 MB, it goes stale with the ETS
# schedule, and rebuilding it should not mean rebuilding an image. Mount it:
#
#   docker run -d --name rideiq-otp --network appnet \
#     -v /opt/rideiq/otp:/var/otp ghcr.io/devdenlab/rideiq-app-otp:latest
#
# where /opt/rideiq/otp holds graph.obj and router-config.json.
FROM eclipse-temurin:25-jre

# OTP 2.9's jar is class-file version 69, so the JRE tag above must stay >= 25.
ARG OTP_VERSION=2.9.0
ARG OTP_URL=https://github.com/opentripplanner/OpenTripPlanner/releases/download/v${OTP_VERSION}/otp-shaded-${OTP_VERSION}.jar

WORKDIR /otp
# curl stays in the image after the download: the deploy's readiness probe runs
# as `docker exec rideiq-otp curl ...`, and OTP loading a graph takes long enough
# that "did it come up?" needs a real answer, not a sleep.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && curl -fsSL -o /otp/otp-shaded.jar "${OTP_URL}" \
    && rm -rf /var/lib/apt/lists/*

# Shipped as a fallback: a mounted router-config.json in /var/otp wins over this.
COPY otp-config/router-config.json /otp/router-config.json

# The EC2 host runs this alongside two FastAPI replicas and redis on 8 GB, so the
# heap is deliberately modest. Edmonton's graph loads comfortably inside it.
ENV OTP_HEAP=3G
EXPOSE 8080

# Private to the docker network — nginx never proxies here, only FastAPI calls it.
CMD ["sh", "-c", "cp -n /otp/router-config.json /var/otp/router-config.json 2>/dev/null; exec java -Xmx${OTP_HEAP} -jar /otp/otp-shaded.jar --load --port 8080 /var/otp"]
