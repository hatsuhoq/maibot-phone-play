"""手机使用周报：最近 7 天玩了多久、玩了什么、看了什么，排成一张卡片交给宿主渲染成图。"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape
from typing import Any, Dict, List, Sequence, Tuple

from .memory import MemoryItem
from .store import SessionRecord

REPORT_DAYS = 7
# 卡片宽度（CSS 像素）；渲染时按 2 倍像素比截图，发出去的图宽 1440
CARD_WIDTH = 720
_TOP_APPS = 5
_WATCHED_LIMIT = 5
_QUOTE_LIMIT = 3
_TITLE_LIMIT = 26
_QUOTE_CHARS = 40
_WEEKDAYS = "一二三四五六日"


@dataclass(frozen=True)
class WeeklyReport:
    """最近 7 天（含今天）的手机使用情况。"""

    nickname: str
    # 每天玩了几分钟，从最早那天到今天
    days: List[Tuple[date, int]]
    sessions: int
    minutes: int
    # 玩得最久的几个应用和分钟数
    apps: List[Tuple[str, int]]
    # 怎么开始的（自己想玩、聊天中决定、桌宠……）和次数
    triggers: List[Tuple[str, int]]
    # 这几天看过、听过的：「哔哩哔哩 · 《……》」
    watched: List[str]
    # 她玩完的感想，最近的在前
    quotes: List[str]

    @property
    def start(self) -> date:
        return self.days[0][0]

    @property
    def end(self) -> date:
        return self.days[-1][0]


def build_report(
    nickname: str,
    daily: Dict[str, Dict[str, Any]],
    history: Sequence[SessionRecord],
    memory: Sequence[MemoryItem],
    today: date,
) -> WeeklyReport:
    """按每天的汇总算次数、时长和应用；看过的内容来自内容记忆，感想来自还留着的游玩记录。"""
    first = today - timedelta(days=REPORT_DAYS - 1)
    days: List[Tuple[date, int]] = []
    sessions = 0
    seconds = 0.0
    apps: Dict[str, float] = {}
    triggers: Dict[str, int] = {}
    for offset in range(REPORT_DAYS):
        day = first + timedelta(days=offset)
        entry = daily.get(day.isoformat())
        if entry is None:
            days.append((day, 0))
            continue
        days.append((day, round(entry["seconds"] / 60)))
        sessions += entry["sessions"]
        seconds += entry["seconds"]
        for name, spent in entry["apps"].items():
            apps[name] = apps.get(name, 0.0) + spent
        for label, count in entry["triggers"].items():
            triggers[label] = triggers.get(label, 0) + count
    top_apps = sorted(apps.items(), key=lambda item: item[1], reverse=True)[:_TOP_APPS]
    return WeeklyReport(
        nickname=nickname,
        days=days,
        sessions=sessions,
        minutes=round(seconds / 60),
        apps=[(name, max(1, round(spent / 60))) for name, spent in top_apps],
        triggers=sorted(triggers.items(), key=lambda item: item[1], reverse=True),
        watched=_watched(memory, first),
        quotes=_quotes(history, first),
    )


def _in_range(stamp: str, first: date) -> bool:
    return bool(stamp) and datetime.fromisoformat(stamp).date() >= first


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _watched(memory: Sequence[MemoryItem], first: date) -> List[str]:
    """这几天看过的视频、在看的小说、在听的歌：看得久的在前，一样久的按最近。"""
    items = [item for item in memory if item.kind in ("watched", "content") and _in_range(item.updated_at, first)]
    items.sort(key=lambda item: (item.seconds, item.updated_at), reverse=True)
    lines: List[str] = []
    for item in items:
        line = f"《{_clip(item.title, _TITLE_LIMIT)}》"
        line = f"{item.app} · {line}" if item.app else line
        if line not in lines:
            lines.append(line)
        if len(lines) == _WATCHED_LIMIT:
            break
    return lines


def _quotes(history: Sequence[SessionRecord], first: date) -> List[str]:
    """玩完的感想取第一句（常用「；」连着好几句），最近的在前、不重复。"""
    quotes: List[str] = []
    for record in reversed(history):
        if not _in_range(record.started_at, first):
            continue
        quote = record.summary.split("；")[0].strip().rstrip("。．.！!？?，,、；; ")
        if quote and quote not in quotes:
            quotes.append(_clip(quote, _QUOTE_CHARS))
        if len(quotes) == _QUOTE_LIMIT:
            break
    return quotes


def _duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    return f"{hours} 小时 {rest} 分" if rest else f"{hours} 小时"


def report_html(report: WeeklyReport) -> str:
    """一张自带样式的卡片（.card），不引用任何外部资源。"""
    busiest = max(minutes for _, minutes in report.days)
    bars = "".join(
        f'<div class="day{" today" if day == report.end else ""}"><span class="value">{minutes or ""}</span>'
        f'<div class="bar" style="height:{4 + (116 * minutes // busiest if busiest else 0)}px"></div></div>'
        for day, minutes in report.days
    )
    labels = "".join(
        f"<span>{'今天' if day == report.end else '周' + _WEEKDAYS[day.weekday()]}</span>" for day, _ in report.days
    )
    longest = report.apps[0][1] if report.apps else 1
    apps = "".join(
        f'<div class="app"><span class="name">{escape(name)}</span>'
        f'<div class="track"><div class="fill" style="width:{max(4, 100 * minutes // longest)}%"></div></div>'
        f'<span class="minutes">{_duration(minutes)}</span></div>'
        for name, minutes in report.apps
    )
    triggers = "".join(f'<span class="chip">{escape(label)} {count}</span>' for label, count in report.triggers)
    sections = [f'<h2>玩得最多</h2><div class="apps">{apps}</div><div class="chips">{triggers}</div>']
    if report.watched:
        items = "".join(f"<li>{escape(line)}</li>" for line in report.watched)
        sections.append(f'<h2>看过的</h2><ul class="watched">{items}</ul>')
    if report.quotes:
        items = "".join(f"<p>「{escape(quote)}」</p>" for quote in report.quotes)
        sections.append(f'<h2>{escape(report.nickname)}说</h2><div class="quotes">{items}</div>')
    average = round(report.minutes / REPORT_DAYS)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: transparent;
  font-family: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif; }}
.card {{ width: {CARD_WIDTH}px; padding: 36px 40px 26px; border-radius: 28px; color: #2d2623;
  background: linear-gradient(160deg, #fff4ec 0%, #ffffff 48%, #f2f6ff 100%); }}
.head {{ display: flex; justify-content: space-between; align-items: baseline; }}
.title {{ font-size: 30px; font-weight: 700; letter-spacing: 1px; }}
.range {{ color: #8b807a; font-size: 16px; }}
.stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-top: 22px; }}
.stat {{ background: #fff; border-radius: 18px; padding: 14px 18px; box-shadow: 0 2px 12px rgba(90, 55, 30, .07); }}
.stat b {{ display: block; font-size: 26px; color: #e86f45; font-weight: 700; }}
.stat span {{ color: #8b807a; font-size: 14px; }}
h2 {{ font-size: 17px; margin: 26px 0 12px; }}
.chart {{ display: flex; gap: 10px; align-items: flex-end; height: 168px; padding: 0 6px;
  border-bottom: 1px solid #efe4dd; }}
.day {{ flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; }}
.day .value {{ font-size: 13px; color: #8b807a; min-height: 18px; }}
.day .bar {{ width: 36px; border-radius: 10px 10px 3px 3px; background: #ffd5c3; margin-top: 4px; }}
.day.today .bar {{ background: #ff8a5b; }}
.labels {{ display: flex; gap: 10px; padding: 6px 6px 0; }}
.labels span {{ flex: 1; text-align: center; font-size: 13px; color: #8b807a; }}
.app {{ display: flex; align-items: center; gap: 12px; margin: 8px 0; font-size: 15px; }}
.app .name {{ width: 120px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.app .track {{ flex: 1; height: 12px; border-radius: 6px; background: #f6ece6; overflow: hidden; }}
.app .fill {{ height: 100%; border-radius: 6px; background: linear-gradient(90deg, #ffb08f, #ff8a5b); }}
.app .minutes {{ width: 96px; text-align: right; color: #8b807a; font-size: 14px; }}
.chips {{ margin-top: 12px; display: flex; flex-wrap: wrap; gap: 8px; }}
.chip {{ font-size: 13px; color: #6f625b; background: #fff; border: 1px solid #f0e2d9; border-radius: 999px;
  padding: 3px 12px; }}
.watched {{ list-style: none; }}
.watched li {{ font-size: 15px; padding: 6px 0; border-bottom: 1px dashed #efe4dd;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.watched li:last-child {{ border-bottom: none; }}
.quotes p {{ font-size: 15px; line-height: 1.6; background: #fff; border-radius: 14px; padding: 10px 14px;
  margin-bottom: 8px; color: #4a3f3a; }}
.foot {{ margin-top: 20px; text-align: right; font-size: 12px; color: #b3a69f; }}
</style></head><body><div class="card">
<div class="head"><div class="title">{escape(report.nickname)}的手机周报</div>
<div class="range">{report.start:%m.%d} – {report.end:%m.%d}</div></div>
<div class="stats">
<div class="stat"><b>{report.sessions} 次</b><span>拿起手机</span></div>
<div class="stat"><b>{_duration(report.minutes)}</b><span>一共玩了</span></div>
<div class="stat"><b>{_duration(average)}</b><span>平均每天</span></div>
</div>
<h2>每天玩了几分钟</h2>
<div class="chart">{bars}</div>
<div class="labels">{labels}</div>
{"".join(sections)}
<div class="foot">刷会儿手机 · 最近 {REPORT_DAYS} 天</div>
</div></body></html>"""
