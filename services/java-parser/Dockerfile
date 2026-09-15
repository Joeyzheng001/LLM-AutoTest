# 对应 R2：Day 1–2 攻坚服务（02 立项排期）。
# 多阶段构建：不依赖本机 JDK/Maven，CI 与服务器均可直接 docker build
FROM maven:3.9-eclipse-temurin-21 AS build

WORKDIR /build
COPY pom.xml checkstyle.xml ./
COPY src ./src
RUN mvn -B -ntp package

FROM eclipse-temurin:21-jre

WORKDIR /app
COPY --from=build /build/target/java-parser-0.1.0.jar app.jar

EXPOSE 8080

# 健康检查依赖 wget（编排口径见 deploy/docker-compose.yml）
RUN apt-get update && apt-get install -y --no-install-recommends wget && rm -rf /var/lib/apt/lists/*

ENTRYPOINT ["java", "-jar", "/app/app.jar"]
