from __future__ import annotations

from pathlib import Path
import shutil
import subprocess


class CertificateError(RuntimeError):
    pass


def _openssl() -> str:
    candidates = [
        shutil.which("openssl.exe"),
        shutil.which("openssl"),
        r"C:\Program Files\Git\usr\bin\openssl.exe",
        r"C:\Program Files\Git\mingw64\bin\openssl.exe",
        r"C:\Program Files\OpenSSL-Win64\bin\openssl.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise CertificateError("HTTPS requires locally installed OpenSSL (Git for Windows includes it).")


def ensure_certificate(directory: Path, ip: str) -> tuple[Path, Path]:
    import ipaddress

    address = str(ipaddress.IPv4Address(ip))
    directory.mkdir(parents=True, exist_ok=True)
    names = {
        "ca_cfg": directory / "ca.cnf",
        "ca_key": directory / "ca-private.key.pem",
        "ca_cert": directory / "ca.crt.pem",
        "ca_der": directory / "testonline-root.cer",
        "server_cfg": directory / "server.cnf",
        "server_req": directory / "server.csr.pem",
        "server_ext": directory / "server.ext.cnf",
        "server_key": directory / "server-private.key.pem",
        "server_cert": directory / "server.crt.pem",
    }

    openssl = _openssl()

    def run(*args: str) -> str:
        try:
            result = subprocess.run(
                [openssl, *args],
                cwd=str(directory),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=45,
                check=True,
                shell=False,
            )
            return result.stdout
        except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            raise CertificateError("OpenSSL certificate preparation failed: " + str(exc)[:250]) from exc

    key = names["server_key"]
    cert = names["server_cert"]
    ca = names["ca_cert"]
    ca_der = names["ca_der"]
    if all(p.is_file() for p in (key, cert, ca, ca_der)):
        out = run("verify", "-CAfile", str(ca), "-purpose", "sslserver", "-verify_ip", address, str(cert))
        if "OK" not in out:
            raise CertificateError("The existing HTTPS certificate does not match the bind IP.")
        run("x509", "-checkend", "86400", "-noout", "-in", str(cert))
        return cert, key
    if any(p.exists() for p in names.values()):
        raise CertificateError("Incomplete certificate set found: stop and inspect; will not overwrite keys.")

    names["ca_cfg"].write_text(
        "[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=v3_ca\n"
        "[dn]\nCN=FolderBridge TestOnline LAN CA\n"
        "[v3_ca]\nbasicConstraints=critical,CA:TRUE,pathlen:0\n"
        "keyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n",
        encoding="utf-8",
    )
    names["server_cfg"].write_text(
        "[req]\nprompt=no\ndistinguished_name=dn\nreq_extensions=leaf_req\n"
        "[dn]\nCN=" + address + "\n[leaf_req]\nsubjectAltName=IP:" + address + "\n",
        encoding="utf-8",
    )
    names["server_ext"].write_text(
        "[server_ext]\nbasicConstraints=critical,CA:FALSE\n"
        "subjectAltName=IP:" + address + "\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid,issuer\n",
        encoding="utf-8",
    )
    run("req", "-x509", "-newkey", "rsa:3072", "-nodes", "-sha256", "-days", "825",
        "-keyout", str(names["ca_key"]), "-out", str(ca), "-config", str(names["ca_cfg"]))
    run("req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256",
        "-keyout", str(key), "-out", str(names["server_req"]), "-config", str(names["server_cfg"]))
    run("x509", "-req", "-in", str(names["server_req"]), "-CA", str(ca),
        "-CAkey", str(names["ca_key"]), "-CAcreateserial",
        "-out", str(cert), "-days", "365", "-sha256",
        "-extfile", str(names["server_ext"]), "-extensions", "server_ext")
    run("x509", "-in", str(ca), "-outform", "DER", "-out", str(ca_der))
    result = run("verify", "-CAfile", str(ca), "-purpose", "sslserver", "-verify_ip", address, str(cert))
    if "OK" not in result:
        raise CertificateError("Generated certificate failed verification.")
    return cert, key
