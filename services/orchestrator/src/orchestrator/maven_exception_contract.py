"""Conservative exception response evidence from locally cached Maven bytecode.

Only a directly declared dependency, its local JAR, and an enabled Spring Boot
auto-configuration can establish this contract. The deployed binary remains
unverified until the runtime exposes a digest or version.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile


_GROUP = "com.htffund.paas"
_COMMON = "commons-utils"
_STARTER = "actuator-spring-cloud-starter"
_HANDLER = "com.htffund.paas.common.exception.CommonExceptionHandler"
_SERVICE_CONFIG = "com.htffund.paas.actuator.service.ServiceAutoConfiguration"
_ENUM = "com.htffund.paas.common.enums.ResponseCode"
_RETURN_TYPE = "com.htffund.paas.common.enums.ReturnType"
_RESPONSE = "com.htffund.paas.common.model.Response"
_AUTO_IMPORTS = "META-INF/spring/org.springframework.boot.autoconfigure.AutoConfiguration.imports"
_MESSAGE = "系统繁忙，请稍后再试"


def _javap_binary() -> str | None:
    candidates = [
        str(Path(os.environ["JAVA_HOME"]) / "bin" / "javap") if os.environ.get("JAVA_HOME") else "",
        shutil.which("javap") or "",
        "/opt/homebrew/opt/openjdk/bin/javap",
        "/usr/local/opt/openjdk/bin/javap",
    ]
    for candidate in dict.fromkeys(filter(None, candidates)):
        if not Path(candidate).is_file():
            continue
        try:
            result = subprocess.run([candidate, "-version"], capture_output=True, text=True, timeout=4)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return candidate
    return None


def _decompile(javap: str, jar: Path, classname: str) -> str | None:
    try:
        result = subprocess.run(
            [javap, "-classpath", str(jar), "-c", "-p", "-v", classname],
            capture_output=True, text=True, timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode or not result.stdout.startswith("Classfile jar:"):
        return None
    return result.stdout


def _method_block(disassembly: str, name: str) -> str | None:
    match = re.search(
        r"^  public .*\b" + re.escape(name) + r"\([^\n]*\);\n"
        r"(?P<body>.*?)(?=^  (?:public|private|protected|static) |^}\s*$)",
        disassembly, re.MULTILINE | re.DOTALL,
    )
    return match.group(0) if match else None


def _declared_dependencies(root: Path) -> set[str]:
    declared: set[str] = set()
    for pom in [root / "pom.xml", *sorted(root.glob("*/pom.xml"))]:
        if not pom.is_file():
            continue
        try:
            tree = ET.parse(pom)
        except (ET.ParseError, OSError):
            continue
        namespace = {"m": "http://maven.apache.org/POM/4.0.0"}
        for dependency in tree.findall("./m:dependencies/m:dependency", namespace):
            group = dependency.findtext("m:groupId", default="", namespaces=namespace)
            artifact = dependency.findtext("m:artifactId", default="", namespaces=namespace)
            scope = dependency.findtext("m:scope", default="", namespaces=namespace)
            if group == _GROUP and scope not in {"test", "provided"}:
                declared.add(artifact)
    return declared


def _managed_version(root: Path) -> str | None:
    try:
        tree = ET.parse(root / "pom.xml")
    except (ET.ParseError, OSError):
        return None
    namespace = {"m": "http://maven.apache.org/POM/4.0.0"}
    version = tree.findtext("./m:properties/m:htf.project.version", namespaces=namespace)
    parent_artifact = tree.findtext("./m:parent/m:artifactId", namespaces=namespace)
    parent_version = tree.findtext("./m:parent/m:version", namespaces=namespace)
    if not version or not parent_version or parent_artifact != "htf-biz-app-parent":
        return None
    repository = Path(os.environ.get("MAVEN_REPOSITORY", str(Path.home() / ".m2" / "repository")))
    parent_pom = (repository / "com" / "htffund" / "paas" / parent_artifact
                  / parent_version / f"{parent_artifact}-{parent_version}.pom")
    try:
        parent_tree = ET.parse(parent_pom)
    except (ET.ParseError, OSError):
        return None
    managed_artifact = parent_tree.findtext("./m:parent/m:artifactId", namespaces=namespace)
    managed_version = parent_tree.findtext("./m:parent/m:version", namespaces=namespace)
    if managed_artifact != "htf-framework-dependency-parent" or not managed_version:
        return None
    management_pom = (repository / "com" / "htffund" / "paas" / managed_artifact
                      / managed_version / f"{managed_artifact}-{managed_version}.pom")
    try:
        management_tree = ET.parse(management_pom)
    except (ET.ParseError, OSError):
        return None
    default_profile = next((profile for profile in management_tree.findall("./m:profiles/m:profile", namespace)
                            if profile.findtext("m:id", namespaces=namespace) == "default"
                            and profile.findtext("m:activation/m:activeByDefault", namespaces=namespace) == "true"), None)
    if default_profile is None:
        return None
    properties: dict[str, str] = {}
    for source in (management_tree.find("./m:properties", namespace),
                   default_profile.find("m:properties", namespace),
                   tree.find("./m:properties", namespace)):
        if source is not None:
            properties.update({entry.tag.rsplit("}", 1)[-1]: entry.text or "" for entry in source})
    resolved: dict[str, str] = {}
    for dependency in management_tree.findall("./m:dependencyManagement/m:dependencies/m:dependency", namespace):
        if dependency.findtext("m:groupId", namespaces=namespace) != _GROUP:
            continue
        artifact = dependency.findtext("m:artifactId", namespaces=namespace)
        if artifact not in {_COMMON, _STARTER}:
            continue
        raw = dependency.findtext("m:version", namespaces=namespace) or ""
        key = raw[2:-1] if raw.startswith("${") and raw.endswith("}") else ""
        resolved[artifact] = properties.get(key, "") if key else raw
    if resolved.get(_COMMON) != version or resolved.get(_STARTER) != version:
        return None
    return version


def _cached_jar(artifact: str, version: str) -> Path | None:
    repository = Path(os.environ.get("MAVEN_REPOSITORY", str(Path.home() / ".m2" / "repository")))
    jar = repository / "com" / "htffund" / "paas" / artifact / version / f"{artifact}-{version}.jar"
    return jar if jar.is_file() else None


def inspect_common_exception_contract(
    root: Path, repo_name: str, interface_ids: list[str],
) -> dict[str, Any] | None:
    """Return a contract only when the exception, enum, and import chain agree."""
    if not interface_ids or not {_COMMON, _STARTER}.issubset(_declared_dependencies(root)):
        return None
    version = _managed_version(root)
    if not version:
        return None
    common = _cached_jar(_COMMON, version)
    starter = _cached_jar(_STARTER, version)
    javap = _javap_binary()
    if not common or not starter or not javap:
        return None
    try:
        with ZipFile(starter) as archive:
            imports = archive.read(_AUTO_IMPORTS).decode("utf-8")
        with ZipFile(common) as archive:
            names = set(archive.namelist())
            required = {name.replace(".", "/") + ".class"
                        for name in (_HANDLER, _ENUM, _RETURN_TYPE, _RESPONSE)}
            if not required.issubset(names):
                return None
    except (BadZipFile, KeyError, OSError, UnicodeDecodeError):
        return None
    if _SERVICE_CONFIG not in imports.splitlines():
        return None
    handler = _decompile(javap, common, _HANDLER)
    codes = _decompile(javap, common, _ENUM)
    return_types = _decompile(javap, common, _RETURN_TYPE)
    response = _decompile(javap, common, _RESPONSE)
    configuration = _decompile(javap, starter, _SERVICE_CONFIG)
    if not all((handler, codes, return_types, response, configuration)):
        return None
    method = _method_block(handler, "handleException")
    setter = _method_block(response, "setResponse")
    constructor = _method_block(response, "Response")
    if not method or not setter or not constructor:
        return None
    if not (
        "org.springframework.web.bind.annotation.ControllerAdvice" in handler
        and "org.springframework.web.bind.annotation.ExceptionHandler(" in method
        and "value=[class Ljava/lang/Throwable;]" in method
        and "ResponseCode.COMMON_ERROR" in method
        and "Response.setResponse:" in method
        and "HttpStatus.OK" in method
        and "ResponseEntity." in method
        and "org.springframework.context.annotation.Import(" in configuration
        and "CommonExceptionHandler;" in configuration
    ):
        return None
    enum_block = codes.split("  static {};", 1)[-1]
    marker = enum_block.find("String COMMON_ERROR")
    finish = enum_block.find("putstatic", marker)
    common_code = enum_block[marker:finish + 200] if marker >= 0 and finish >= 0 else ""
    if not (
        "sipush        9999" in common_code
        and f"String {_MESSAGE}" in common_code
        and "ReturnType.T" in common_code
        and "Field COMMON_ERROR" in common_code
    ):
        return None
    type_block = return_types.split("  static {};", 1)[-1]
    type_marker = type_block.find("String T")
    type_finish = type_block.find("putstatic", type_marker)
    technical_type = type_block[type_marker:type_finish + 150] if type_marker >= 0 and type_finish >= 0 else ""
    if not ("String T" in technical_type and "Field T:" in technical_type
            and "Field type:" in return_types and "public java.lang.String getType()" in return_types):
        return None
    if not ("private T body;" in response and "Field body:" not in setter
            and "Field body:" not in constructor
            and "setBody" not in method):
        return None
    digest = hashlib.sha256(common.read_bytes()).hexdigest()
    coordinate = f"{_GROUP}:{_COMMON}:{version}"
    excerpts = [
        ("exception_advice", _COMMON, _HANDLER,
         method + "\norg.springframework.web.bind.annotation.ControllerAdvice"),
        ("dependency_enum", _COMMON, _ENUM, common_code),
        ("dependency_return_type", _COMMON, _RETURN_TYPE,
         technical_type + "\npublic java.lang.String getType();\nField type:"),
        ("dependency_response", _COMMON, _RESPONSE,
         "private T body;\n" + constructor + "\n" + setter),
        ("dependency_activation", _STARTER, _SERVICE_CONFIG,
         _AUTO_IMPORTS + "\n" + _SERVICE_CONFIG + "\n"
         + configuration[configuration.rfind("org.springframework.context.annotation.Import("):
                         configuration.rfind("org.springframework.context.annotation.Import(") + 210]),
    ]
    evidence = [{
        "category": "dependency_bytecode", "repo": repo_name,
        "file": f"{_GROUP}:{artifact}:{version}!/{classname.replace('.', '/')}.class",
        "start_line": 1, "end_line": len(excerpt.splitlines()),
        "excerpt": excerpt, "excerpt_truncated": False,
        "interface_ids": sorted(set(interface_ids)), "role": role,
    } for role, artifact, classname, excerpt in excerpts]
    return {
        "coordinate": coordinate, "sha256": digest,
        "deployment_verified": False, "evidence": evidence,
        "http_status": 200, "return_code": 9999,
        "return_msg": _MESSAGE, "return_type": "T", "body_is_null": True,
    }
