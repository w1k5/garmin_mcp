"""
Women's health functions for Garmin Connect MCP Server
"""
import json
from datetime import date, date as calendar_date, timedelta
from typing import Any, List
from garminconnect import VALID_MENSTRUAL_MOODS, VALID_MENSTRUAL_SEX_DRIVE, VALID_MENSTRUAL_SYMPTOMS

# The garmin_client will be set by the main file
garmin_client = None

MENSTRUAL_CALENDAR_MAX_DAYS = 92


def _unique_items(items: List[Any]) -> List[Any]:
    """Keep identical JSON records once, preserving their first occurrence."""
    seen = set()
    unique = []
    for item in items:
        key = json.dumps(item, sort_keys=True)
        if key not in seen:
            unique.append(item)
            seen.add(key)
    return unique


def _stitch_menstrual_chunks(chunks: List[Any]) -> Any:
    """Combine list-shaped responses; retain other payloads grouped by chunk."""
    if len(chunks) == 1:
        return chunks[0]

    if all(isinstance(chunk, list) for chunk in chunks):
        stitched = []
        for chunk in chunks:
            stitched.extend(chunk)
        return _unique_items(stitched)

    if all(isinstance(chunk, dict) for chunk in chunks) and all(
        isinstance(value, list)
        for chunk in chunks
        for value in chunk.values()
    ):
        stitched = {}
        for chunk in chunks:
            for key, value in chunk.items():
                stitched.setdefault(key, []).extend(value)
        return {key: _unique_items(items) for key, items in stitched.items()}

    return chunks


def configure(client):
    """Configure the module with the Garmin client instance"""
    global garmin_client
    garmin_client = client


def register_tools(app):
    """Register all women's health tools with the MCP server app"""
    
    @app.tool()
    async def get_pregnancy_summary() -> str:
        """Get pregnancy summary data"""
        try:
            summary = garmin_client.get_pregnancy_summary()
            if not summary:
                return "No pregnancy summary data found."
            return json.dumps(summary, indent=2)
        except Exception as e:
            return f"Error retrieving pregnancy summary: {str(e)}"
    
    @app.tool()
    async def get_menstrual_data_for_date(date: str) -> str:
        """Get menstrual data for a specific date
        
        Args:
            date: Date in YYYY-MM-DD format
        """
        try:
            data = garmin_client.get_menstrual_data_for_date(date)
            if not data:
                return f"No menstrual data found for {date}."
            return json.dumps(data, indent=2)
        except Exception as e:
            return f"Error retrieving menstrual data: {str(e)}"
    
    @app.tool()
    async def log_menstrual_symptoms(
        date: str, symptoms: list[str], note: str = "", moods: list[str] | None = None
    ) -> str:
        """Add symptoms or moods to a Garmin menstrual daily log, preserving existing entries.

        Args:
            date: Local calendar date in YYYY-MM-DD format.
            symptoms: Garmin symptom names: ACNE, BACKACHE, BLOATING,
                BODY_ACHES, CONSTIPATION, CRAMPS, CRAVINGS, DIARRHEA,
                DIZZINESS, FATIGUE, HEADACHE, INSOMNIA, NAUSEA,
                TENDER_BREASTS. Existing symptoms are retained.
            note: Optional free-text detail, including symptoms Garmin does not
                categorize. Appended to any existing note.
            moods: Optional Garmin mood names: ENERGETIC, EMOTIONAL, FINE,
                FRUSTRATED, HAPPY, IRRITABLE, MOOD_SWINGS, MOTIVATED,
                OVERWHELMED, SAD. Existing moods are retained.
        """
        try:
            calendar_date.fromisoformat(date)
            normalized = [symptom.strip().upper().replace(" ", "_") for symptom in symptoms]
            invalid = sorted(set(normalized) - VALID_MENSTRUAL_SYMPTOMS)
            if invalid:
                return f"Unsupported Garmin symptoms: {', '.join(invalid)}."
            normalized_moods = [mood.strip().upper().replace(" ", "_") for mood in (moods or [])]
            invalid_moods = sorted(set(normalized_moods) - VALID_MENSTRUAL_MOODS)
            if invalid_moods:
                return f"Unsupported Garmin moods: {', '.join(invalid_moods)}."
            if not normalized and not normalized_moods and not note.strip():
                return "Provide at least one symptom, mood, or a note."

            day = garmin_client.get_menstrual_data_for_date(date)
            if not isinstance(day, dict):
                return f"Could not read the menstrual log for {date}; no changes made."
            existing = day.get("dayLog") or {}
            if not isinstance(existing, dict):
                return f"Could not read the menstrual log for {date}; no changes made."
            merged_symptoms = list(dict.fromkeys([*(existing.get("symptoms") or []), *normalized]))
            merged_moods = list(dict.fromkeys([*(existing.get("moods") or []), *normalized_moods]))
            prior_note = existing.get("notes") or ""
            added_note = note.strip()
            merged_note = "\n".join(part for part in (prior_note, added_note) if part)

            garmin_client.update_menstrual_daily_log(
                date,
                symptoms=merged_symptoms,
                moods=merged_moods,
                flow=existing.get("flow"),
                discharge=existing.get("discharge"),
                sex_drive=existing.get("sexDrive"),
                sexual_activity=existing.get("sexualActivity"),
                notes=merged_note if added_note else None,
                ovulation_day=existing.get("ovulationDay"),
            )
            saved = garmin_client.get_menstrual_data_for_date(date)
            saved_log = saved.get("dayLog") if isinstance(saved, dict) else None
            if not isinstance(saved_log, dict) or not set(merged_symptoms).issubset(
                saved_log.get("symptoms") or []
            ) or not set(merged_moods).issubset(saved_log.get("moods") or []) or (
                added_note and merged_note != saved_log.get("notes")
            ):
                return f"Garmin accepted the request, but the saved log for {date} could not be verified."
            return json.dumps({"date": date, "symptoms": saved_log.get("symptoms") or [], "moods": saved_log.get("moods") or [], "notes": saved_log.get("notes")}, indent=2)
        except ValueError as e:
            return f"Invalid menstrual log input: {e}"
        except Exception as e:
            return f"Error logging menstrual symptoms: {e}"

    @app.tool()
    async def log_menstrual_mood(date: str, moods: list[str], note: str = "") -> str:
        """Add Garmin menstrual mood categories without removing existing daily details.

        Args:
            date: Local calendar date in YYYY-MM-DD format.
            moods: ENERGETIC, EMOTIONAL, FINE, FRUSTRATED, HAPPY, IRRITABLE,
                MOOD_SWINGS, MOTIVATED, OVERWHELMED, or SAD.
            note: Optional exact wording for a mood Garmin does not categorize,
                appended to the existing note.
        """
        return await log_menstrual_symptoms(date, [], note, moods)

    @app.tool()
    async def set_menstrual_sex_drive(date: str, sex_drive: str) -> str:
        """Set Garmin's Sex Drive field for one day, preserving other daily details.

        Args:
            date: Local calendar date in YYYY-MM-DD format.
            sex_drive: LOW, AVERAGE, or HIGH.
        """
        try:
            calendar_date.fromisoformat(date)
            value = sex_drive.strip().upper()
            if value not in VALID_MENSTRUAL_SEX_DRIVE:
                return "Unsupported Sex Drive value. Use LOW, AVERAGE, or HIGH."
            day = garmin_client.get_menstrual_data_for_date(date)
            if not isinstance(day, dict):
                return f"Could not read the menstrual log for {date}; no changes made."
            existing = day.get("dayLog") or {}
            if not isinstance(existing, dict):
                return f"Could not read the menstrual log for {date}; no changes made."
            garmin_client.update_menstrual_daily_log(
                date,
                symptoms=existing.get("symptoms") or [],
                moods=existing.get("moods") or [],
                flow=existing.get("flow"),
                discharge=existing.get("discharge") or [],
                sex_drive=value,
                sexual_activity=existing.get("sexualActivity"),
                notes=existing.get("notes"),
                ovulation_day=existing.get("ovulationDay"),
            )
            saved = garmin_client.get_menstrual_data_for_date(date)
            saved_log = saved.get("dayLog") if isinstance(saved, dict) else None
            if not isinstance(saved_log, dict) or saved_log.get("sexDrive") != value:
                return f"Garmin accepted the request, but the saved Sex Drive value for {date} could not be verified."
            return json.dumps({"date": date, "sexDrive": value}, indent=2)
        except ValueError as e:
            return f"Invalid menstrual log input: {e}"
        except Exception as e:
            return f"Error setting Sex Drive: {e}"

    @app.tool()
    async def get_menstrual_calendar_data(start_date: str, end_date: str) -> str:
        """Get menstrual calendar data between specified dates

        Automatically chunks requests longer than 92 days, Garmin's
        server-side limit, and stitches the responses together.
        Identical records spanning a chunk boundary are returned once for list
        and dict-of-list responses. Other payload shapes are returned per chunk.
        
        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
            if end < start:
                return f"end_date {end_date} is before start_date {start_date}."

            chunks = []
            cursor = start
            while cursor <= end:
                window_end = min(
                    cursor + timedelta(days=MENSTRUAL_CALENDAR_MAX_DAYS - 1),
                    end,
                )
                data = garmin_client.get_menstrual_calendar_data(
                    cursor.isoformat(),
                    window_end.isoformat(),
                )
                if data:
                    chunks.append(data)
                cursor = window_end + timedelta(days=1)

            if not chunks:
                return (
                    f"No menstrual calendar data found between {start_date} "
                    f"and {end_date}."
                )

            return json.dumps(_stitch_menstrual_chunks(chunks), indent=2)
        except Exception as e:
            return f"Error retrieving menstrual calendar data: {str(e)}"

    return app
