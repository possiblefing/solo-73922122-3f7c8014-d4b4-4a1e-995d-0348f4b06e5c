"""Legacy air-quality monitoring portal (local mock).

A deliberately old-fashioned, server-rendered portal used to exercise the
Playwright collector end to end.  No third-party dependencies: only the
Python standard library.

Behaviour that matters for the collector:
  * GET  /            query form (station <select>, month <input type=month>)
  * GET  /query       server-side query, result table + "Export CSV" button
  * GET  /export      opens a new window (window.open), waits POPUP_DELAY
                      seconds, then starts a CSV download after
                      DOWNLOAD_DELAY further seconds
  * GET  /healthz     liveness probe

Delays can be overridden per request (?popup_delay=&download_delay=) so the
collector's 60s timeout and cancellation flows can be demonstrated.
"""

from __future__ import annotations

import csv
import io
import math
import os
import time
import urllib.parse
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Stations with fictional but deterministic readings.  STBAD deliberately
# returns a CSV whose station column does not match the requested station,
# so the collector's row-level validation can be demonstrated.
STATIONS = ["ST001", "ST002", "ST003", "ST004", "ST005", "STBAD"]
BAD_STATION = "STBAD"

DEFAULT_POPUP_DELAY = float(os.environ.get("PORTAL_POPUP_DELAY", "2.0"))
DEFAULT_DOWNLOAD_DELAY = float(os.environ.get("PORTAL_DOWNLOAD_DELAY", "1.5"))
MAX_DELAY = 600.0
LISTEN_PORT = int(os.environ.get("PORTAL_PORT", "8080"))


def clamp_delay(raw: str | None, default: float) -> float:
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(0.0, min(MAX_DELAY, value))


def parse_month(raw: str | None) -> tuple[int, int] | None:
    """Parse an <input type=month> value (YYYY-MM)."""
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m")
    except ValueError:
        return None
    return parsed.year, parsed.month


def readings_for(station: str, year: int, month: int) -> list[tuple[str, float]]:
    """24 hourly fictional PM2.5 readings (UTC) for the given month."""
    rows: list[tuple[str, float]] = []
    for hour in range(24):
        # Deterministic pseudo-random component seeded by station+month+hour.
        seed = sum(ord(c) for c in station) + year * 7 + month * 13 + hour * 31
        wobble = (seed % 17) - 8  # -8 .. 8
        diurnal = 18.0 * math.sin((hour - 7) / 24.0 * 2.0 * math.pi)
        level = 35.0 + diurnal + wobble
        pm25 = round(max(1.0, min(500.0, level)), 1)
        ts = f"{year:04d}-{month:02d}-01T{hour:02d}:00:00Z"
        rows.append((ts, pm25))
    return rows


def build_csv(station: str, year: int, month: int) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(["station", "utc_time", "pm25_ugm3"])
    emitted_station = "ST999" if station == BAD_STATION else station
    for ts, pm25 in readings_for(station, year, month):
        writer.writerow([emitted_station, ts, pm25])
    return buf.getvalue().encode("utf-8")


PAGE_CSS = """
body { font-family: 'Times New Roman', serif; background:#e8ecf2; margin:0; }
table.layout { margin:24px auto; border:2px solid #7f8fa6; background:#ffffff;
  border-collapse:collapse; }
table.layout td { padding:10px 14px; }
.titlebar { background:#2c3e50; color:#fff; font-size:20px; font-weight:bold;
  padding:12px 18px; letter-spacing:1px; }
.label { color:#34495e; font-weight:bold; text-align:right; }
.datatable { border-collapse:collapse; margin-top:8px; }
.datatable th, .datatable td { border:1px solid #95a5a6; padding:3px 10px;
  font-size:13px; }
.datatable th { background:#d5dbdb; }
button.legacy { background:#bdc3c7; border:2px outset #7f8c8d; padding:4px 14px;
  font-family:inherit; font-size:14px; cursor:pointer; }
.err { color:#c0392b; font-weight:bold; }
.ok { color:#1e8449; font-weight:bold; }
.foot { color:#7f8c8d; font-size:11px; margin-top:14px; }
"""


def shell(title: str, body: str) -> bytes:
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{PAGE_CSS}</style>
</head>
<table class="layout" style="width:780px">
  <tr><td class="titlebar" colspan="2">&#127785; 城市空气质量旧监测门户 v2.3 (演示)</td></tr>
  {body}
</table>
</html>"""
    return html.encode("utf-8")


def index_page() -> bytes:
    today = date.today().strftime("%Y-%m")
    options = "\n".join(
        f'      <option value="{s}">{s}</option>' for s in STATIONS
    )
    body = f"""
  <tr><td style="padding:18px">
    <form method="get" action="/query">
      <table>
        <tr>
          <td class="label">监测站号：</td>
          <td><select name="station" size="1">
{options}
          </select></td>
        </tr>
        <tr>
          <td class="label">UTC 月份：</td>
          <td><input type="month" name="month" value="{today}"></td>
        </tr>
        <tr>
          <td colspan="2" style="text-align:center;padding-top:12px">
            <button class="legacy" type="submit">查询数据</button>
          </td>
        </tr>
      </table>
    </form>
    <div class="foot">本门户仅支持网页操作；导出将在新窗口生成 CSV 文件。<br>
      延迟演示参数（可选）：popup_delay / download_delay（秒）。</div>
  </td></tr>"""
    return shell("旧监测门户 - 首页", body)


def query_page(params: dict[str, list[str]]) -> tuple[int, bytes]:
    station = params.get("station", [""])[0]
    month_raw = params.get("month", [""])[0]
    popup_delay = clamp_delay(params.get("popup_delay", [None])[0], DEFAULT_POPUP_DELAY)
    download_delay = clamp_delay(
        params.get("download_delay", [None])[0], DEFAULT_DOWNLOAD_DELAY
    )

    ym = parse_month(month_raw)
    if ym is None:
        body = """<tr><td style="padding:18px">
          <span class="err">错误：月份格式无效，请使用 YYYY-MM。</span>
          <p><a href="/">返回查询页</a></p></td></tr>"""
        return 400, shell("查询失败", body)

    year, month = ym
    if station not in STATIONS:
        body = f"""<tr><td style="padding:18px">
          <span class="err">错误：监测站号 {station} 不存在或已停用。</span>
          <p><a href="/">返回查询页</a></p></td></tr>"""
        return 404, shell("站点不存在", body)

    rows = readings_for(station, year, month)
    data_rows = "\n".join(
        f'<tr><td>{station}</td><td>{ts}</td><td>{pm25}</td></tr>'
        for ts, pm25 in rows
    )
    export_href = (
        f"/export?station={urllib.parse.quote(station)}"
        f"&month={urllib.parse.quote(month_raw)}"
        f"&popup_delay={popup_delay}&download_delay={download_delay}"
    )
    body = f"""
  <tr><td style="padding:16px">
    <div class="ok">查询成功：站点 {station}，UTC {year:04d}-{month:02d}，
      共 {len(rows)} 条小时读数。</div>
    <table class="datatable">
      <tr><th>站号</th><th>UTC 采样时间</th><th>PM2.5 (ug/m3)</th></tr>
      {data_rows}
    </table>
    <p style="margin-top:14px">
      <button class="legacy" type="button"
        onclick="window.open('{export_href}','export_win','width=520,height=260')">
        导出 CSV（新窗口）</button>
      &nbsp;<a href="/">重新查询</a>
    </p>
    <div class="foot">点击后新窗口将延迟 {popup_delay:.1f}s 打开，
      下载再延迟 {download_delay:.1f}s 开始。</div>
  </td></tr>"""
    return 200, shell("查询结果", body)


def export_landing_html(station: str, month: str, download_delay: float) -> bytes:
    body = f"""
  <tr><td style="padding:18px;text-align:center">
    <p>正在为站点 <b>{station}</b>（{month}）准备导出文件……</p>
    <p class="foot">下载将于 {download_delay:.1f} 秒后自动开始。</p>
    <script>
      setTimeout(function () {{
        window.location.href = '/export?station={urllib.parse.quote(station)}'
          + '&month={urllib.parse.quote(month)}'
          + '&download_delay={download_delay}'
          + '&stage=download';
      }}, {int(download_delay * 1000)});
    </script>
  </td></tr>"""
    return shell("导出窗口", body)


class PortalHandler(BaseHTTPRequestHandler):
    server_version = "LegacyAQPortal/2.3"

    def log_message(self, fmt: str, *args) -> None:  # quieter logs
        print(f"[portal] {self.address_string()} - {fmt % args}")

    def _send(self, status: int, body: bytes, content_type: str,
              extra_headers: list[tuple[str, str]] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in extra_headers or []:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        path = parsed.path

        if path == "/healthz":
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return

        if path == "/":
            self._send(200, index_page(), "text/html; charset=utf-8")
            return

        if path == "/query":
            status, body = query_page(params)
            self._send(status, body, "text/html; charset=utf-8")
            return

        if path == "/export":
            self._handle_export(params)
            return

        self._send(404, shell("未找到", '<tr><td style="padding:18px">'
                  '<span class="err">404 - 页面不存在。</span></td></tr>'),
                  "text/html; charset=utf-8")

    def _handle_export(self, params: dict[str, list[str]]) -> None:
        station = params.get("station", [""])[0]
        month_raw = params.get("month", [""])[0]
        stage = params.get("stage", ["popup"])[0]
        popup_delay = clamp_delay(params.get("popup_delay", [None])[0],
                                  DEFAULT_POPUP_DELAY)
        download_delay = clamp_delay(
            params.get("download_delay", [None])[0], DEFAULT_DOWNLOAD_DELAY
        )
        ym = parse_month(month_raw)

        if ym is None or station not in STATIONS:
            body = shell("导出失败",
                         '<tr><td style="padding:18px"><span class="err">'
                         '导出失败：站号或月份参数无效。</span></td></tr>')
            self._send(400, body, "text/html; charset=utf-8")
            return

        year, month = ym

        if stage != "download":
            # First stage: emulate the portal taking time before the popup
            # document becomes ready.
            time.sleep(popup_delay)
            self._send(200, export_landing_html(station, month_raw,
                                                download_delay),
                       "text/html; charset=utf-8")
            return

        # Second stage: emulate server-side report generation, then download.
        time.sleep(download_delay)
        payload = build_csv(station, year, month)
        filename = f"aq_{station}_{year:04d}{month:02d}.csv"
        headers = [
            ("Content-Disposition",
             f'attachment; filename="{filename}"'),
        ]
        self._send(200, payload, "text/csv; charset=utf-8", headers)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), PortalHandler)
    print(f"[portal] legacy air-quality portal listening on :{LISTEN_PORT} "
          f"(popup={DEFAULT_POPUP_DELAY}s, download={DEFAULT_DOWNLOAD_DELAY}s)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
