"""Read-only data views behind the standard backend port."""
from datetime import date
import json


class Views:
    def __init__(self, data, sources):
        self.data = data
        self.sources = sources

    def handle(self, operation, value):
        if operation == "data.status":
            return self.data.profile_report()
        if operation == "data.research_inputs":
            return self.data.research_report()
        if operation == "data.search":
            return self.data.search(value["q"])
        if operation == "data.bars":
            return self.bars(value)
        if operation == "data.check":
            return self.data.doctor_report()
        raise ValueError(f"unsupported data operation: {operation}")

    def query(self, value):
        from protocol import DataQuery
        dataset = value["dataset_id"]
        fields = tuple(value.get("fields", ("open", "high", "low", "close", "volume", "amount")))
        symbols = value.get("symbols")
        query = DataQuery(dataset, date.fromisoformat(value["start"]),
                          date.fromisoformat(value["end"]), fields,
                          int(value.get("history", 1)), value.get("price_basis", "raw"),
                          symbols=None if symbols is None else tuple(symbols),
                          universe=value.get("universe", "dataset"))
        result = self.data.query(query)
        rows = []
        for record in result.records:
            for symbol, values in record.frame.fields.items():
                rows.append({"code": symbol, "date": record.trade_date.isoformat(), **values})
        return {"items": rows}


    def bars(self, value):
        frequencies = {"分时", "日", "周", "月", "年"}
        adjustments = {"不复权", "前复权", "后复权"}
        freq, adj = value.get("freq", "日"), value.get("adj", "不复权")
        if freq not in frequencies or adj not in adjustments:
            raise ValueError("非法参数")
        if freq == "分时":
            source = self.sources.select(mode="kline", data_type="minute")
            if source.get("frequency") not in {"minute", "intraday"}:
                raise ValueError("分时源频率无效")
            raise ValueError("分时源尚未提供标准查询字段")
        source = self.sources.select(mode="kline", data_type="daily")
        dataset = source.get("dataset_id")
        calendar_name = source.get("calendar")
        if not isinstance(dataset, str) or not isinstance(calendar_name, str):
            raise ValueError("daily kline source must declare dataset_id and calendar")
        calendar = json.loads(self.data.read("sources", dataset, calendar_name))
        sessions = [date.fromisoformat(str(item)) for item in calendar.get("sessions", ())]
        if len(sessions) < 2:
            raise ValueError("daily kline calendar is missing")
        value = {**value, "dataset_id": dataset, "symbols": [value["code"]],
                 "start": value.get("start", sessions[0].isoformat()),
                 "end": value.get("end", sessions[-2].isoformat())}
        value["price_basis"] = "raw" if adj == "不复权" else "adjusted"
        result = self.query(value)
        rows = result["items"]
        rows.sort(key=lambda row: row["date"])
        if freq != "日":
            groups = {}
            for row in rows:
                day = date.fromisoformat(row["date"])
                if freq == "周":
                    key = day.isocalendar()[:2]
                elif freq == "月":
                    key = (day.year, day.month)
                else:
                    key = (day.year,)
                groups.setdefault(key, []).append(row)
            rows = []
            for values in groups.values():
                values.sort(key=lambda item: item["date"])
                first, last = values[0], values[-1]
                rows.append({"date": last["date"],
                             "open": first.get("open"),
                             "high": max(item["high"] for item in values),
                             "low": min(item["low"] for item in values),
                             "close": last.get("close"),
                             "volume": sum(item.get("volume") or 0 for item in values),
                             "amount": sum(item.get("amount") or 0 for item in values)})
        return {
            "dates": [row["date"] for row in rows],
            "candles": [[row.get("open"), row.get("close"), row.get("low"), row.get("high")]
                        for row in rows],
            "volumes": [row.get("volume") for row in rows],
        }
