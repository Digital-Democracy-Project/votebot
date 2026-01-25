"""
State Legislative Session Date Lookup.

Provides start and end dates for state legislative sessions based on state code and year.
Calculates actual Monday start dates and Friday end dates.
"""

from datetime import date, timedelta
from typing import Any


class StateLegislativeCalendar:
    """
    A class to lookup state legislative session dates based on state ISO2 code and year.
    Calculates actual Monday start and Friday end dates.
    """

    def __init__(self):
        # State legislative session data with patterns
        # Format: 'STATE_CODE': {
        #   'odd_year': {'start_pattern': 'pattern', 'duration_weeks': int, 'notes': ''},
        #   'even_year': {'start_pattern': 'pattern', 'duration_weeks': int, 'notes': ''},
        #   'full_time': bool,
        #   'biennial_odd_only': bool
        # }
        #
        # Start patterns:
        # - 'first_monday_january' - First Monday in January
        # - 'second_monday_january' - Second Monday in January
        # - 'third_monday_january' - Third Monday in January
        # - 'first_monday_february' - First Monday in February
        # - 'second_monday_february' - Second Monday in February
        # - 'first_monday_march' - First Monday in March
        # - 'first_monday_april' - First Monday in April
        # - 'first_monday_december_prev' - First Monday in December of previous year

        self.legislative_data = {
            "AL": {  # Alabama
                "odd_year": {
                    "start_pattern": "first_monday_february",
                    "duration_weeks": 15,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 11,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "AK": {  # Alaska
                "odd_year": {
                    "start_pattern": "third_monday_january",
                    "duration_weeks": 17,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "third_monday_january",
                    "duration_weeks": 17,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "AZ": {  # Arizona
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 24,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 15,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "AR": {  # Arkansas
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 16,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "second_monday_april",
                    "duration_weeks": 4,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "CA": {  # California
                "odd_year": {
                    "start_pattern": "first_monday_december_prev",
                    "duration_weeks": 40,
                    "notes": "Full-time legislature",
                },
                "even_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 35,
                    "notes": "Full-time legislature",
                },
                "full_time": True,
                "biennial_odd_only": False,
            },
            "CO": {  # Colorado
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 17,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 18,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "CT": {  # Connecticut
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 21,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "first_monday_february",
                    "duration_weeks": 13,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "DE": {  # Delaware
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 24,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 24,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "FL": {  # Florida
                "odd_year": {
                    "start_pattern": "first_monday_march",
                    "duration_weeks": 15,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 9,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "GA": {  # Georgia
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 12,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 13,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "HI": {  # Hawaii
                "odd_year": {
                    "start_pattern": "third_monday_january",
                    "duration_weeks": 15,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "third_monday_january",
                    "duration_weeks": 16,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "ID": {  # Idaho
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 13,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 13,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "IL": {  # Illinois
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 52,
                    "notes": "Full-time legislature",
                },
                "even_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 52,
                    "notes": "Full-time legislature",
                },
                "full_time": True,
                "biennial_odd_only": False,
            },
            "IN": {  # Indiana
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 15,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "first_monday_december_prev",
                    "duration_weeks": 15,
                    "notes": "Starts in December",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "IA": {  # Iowa
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 17,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 15,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "KS": {  # Kansas
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 13,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 13,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "KY": {  # Kentucky
                "odd_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 12,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 15,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "LA": {  # Louisiana
                "odd_year": {
                    "start_pattern": "second_monday_april",
                    "duration_weeks": 9,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "second_monday_march",
                    "duration_weeks": 12,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "ME": {  # Maine
                "odd_year": {
                    "start_pattern": "first_monday_december_prev",
                    "duration_weeks": 15,
                    "notes": "Starts in December",
                },
                "even_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 15,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "MD": {  # Maryland
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 13,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 14,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "MA": {  # Massachusetts
                "odd_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 52,
                    "notes": "Full-time legislature",
                },
                "even_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 52,
                    "notes": "Full-time legislature",
                },
                "full_time": True,
                "biennial_odd_only": False,
            },
            "MI": {  # Michigan
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 52,
                    "notes": "Full-time legislature",
                },
                "even_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 52,
                    "notes": "Full-time legislature",
                },
                "full_time": True,
                "biennial_odd_only": False,
            },
            "MN": {  # Minnesota
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 18,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "third_monday_february",
                    "duration_weeks": 13,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "MS": {  # Mississippi
                "odd_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 13,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 13,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "MO": {  # Missouri
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 18,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 19,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "MT": {  # Montana
                "odd_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 17,
                    "notes": "Meets only in odd years",
                },
                "even_year": {
                    "start_pattern": None,
                    "duration_weeks": 0,
                    "notes": "No regular session in even years",
                },
                "full_time": False,
                "biennial_odd_only": True,
            },
            "NE": {  # Nebraska
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 21,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 15,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "NV": {  # Nevada
                "odd_year": {
                    "start_pattern": "first_monday_february",
                    "duration_weeks": 17,
                    "notes": "Meets only in odd years",
                },
                "even_year": {
                    "start_pattern": None,
                    "duration_weeks": 0,
                    "notes": "No regular session in even years",
                },
                "full_time": False,
                "biennial_odd_only": True,
            },
            "NH": {  # New Hampshire
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 24,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 26,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "NJ": {  # New Jersey
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 52,
                    "notes": "Full-time legislature",
                },
                "even_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 52,
                    "notes": "Full-time legislature",
                },
                "full_time": True,
                "biennial_odd_only": False,
            },
            "NM": {  # New Mexico
                "odd_year": {
                    "start_pattern": "third_monday_january",
                    "duration_weeks": 9,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "third_monday_january",
                    "duration_weeks": 4,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "NY": {  # New York
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 23,
                    "notes": "Full-time legislature",
                },
                "even_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 52,
                    "notes": "Full-time legislature",
                },
                "full_time": True,
                "biennial_odd_only": False,
            },
            "NC": {  # North Carolina
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 52,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "third_monday_april",
                    "duration_weeks": 15,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "ND": {  # North Dakota
                "odd_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 17,
                    "notes": "Meets only in odd years",
                },
                "even_year": {
                    "start_pattern": None,
                    "duration_weeks": 0,
                    "notes": "No regular session in even years",
                },
                "full_time": False,
                "biennial_odd_only": True,
            },
            "OH": {  # Ohio
                "odd_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 52,
                    "notes": "Full-time legislature",
                },
                "even_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 52,
                    "notes": "Full-time legislature",
                },
                "full_time": True,
                "biennial_odd_only": False,
            },
            "OK": {  # Oklahoma
                "odd_year": {
                    "start_pattern": "first_monday_february",
                    "duration_weeks": 17,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "first_monday_february",
                    "duration_weeks": 17,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "OR": {  # Oregon
                "odd_year": {
                    "start_pattern": "third_monday_january",
                    "duration_weeks": 22,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "first_monday_february",
                    "duration_weeks": 5,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "PA": {  # Pennsylvania
                "odd_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 52,
                    "notes": "Full-time legislature",
                },
                "even_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 48,
                    "notes": "Full-time legislature",
                },
                "full_time": True,
                "biennial_odd_only": False,
            },
            "RI": {  # Rhode Island
                "odd_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 24,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 26,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "SC": {  # South Carolina
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 19,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 17,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "SD": {  # South Dakota
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 11,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 11,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "TN": {  # Tennessee
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 14,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 15,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "TX": {  # Texas
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 20,
                    "notes": "Meets only in odd years",
                },
                "even_year": {
                    "start_pattern": None,
                    "duration_weeks": 0,
                    "notes": "No regular session in even years",
                },
                "full_time": False,
                "biennial_odd_only": True,
            },
            "UT": {  # Utah
                "odd_year": {
                    "start_pattern": "third_monday_january",
                    "duration_weeks": 7,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "third_monday_january",
                    "duration_weeks": 7,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "VT": {  # Vermont
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 23,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 18,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "VA": {  # Virginia
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 6,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 9,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "WA": {  # Washington
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 15,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 9,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "WV": {  # West Virginia
                "odd_year": {
                    "start_pattern": "second_monday_february",
                    "duration_weeks": 9,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 9,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
            "WI": {  # Wisconsin
                "odd_year": {
                    "start_pattern": "first_monday_january",
                    "duration_weeks": 52,
                    "notes": "Full-time legislature",
                },
                "even_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 52,
                    "notes": "Full-time legislature",
                },
                "full_time": True,
                "biennial_odd_only": False,
            },
            "WY": {  # Wyoming
                "odd_year": {
                    "start_pattern": "second_monday_january",
                    "duration_weeks": 8,
                    "notes": "",
                },
                "even_year": {
                    "start_pattern": "second_monday_february",
                    "duration_weeks": 4,
                    "notes": "",
                },
                "full_time": False,
                "biennial_odd_only": False,
            },
        }

    def _get_nth_weekday(self, year: int, month: int, weekday: int, n: int) -> date:
        """
        Get the nth occurrence of a weekday in a given month/year.

        Args:
            year: Year
            month: Month (1-12)
            weekday: Weekday (0=Monday, 6=Sunday)
            n: Which occurrence (1=first, 2=second, etc.)

        Returns:
            Date of the nth weekday
        """
        # Get the first day of the month
        first_day = date(year, month, 1)

        # Find the first occurrence of the target weekday
        days_ahead = weekday - first_day.weekday()
        if days_ahead < 0:  # Target day already happened this week
            days_ahead += 7

        first_occurrence = first_day + timedelta(days=days_ahead)

        # Calculate the nth occurrence
        nth_occurrence = first_occurrence + timedelta(weeks=n - 1)

        # Make sure we're still in the same month
        if nth_occurrence.month != month:
            raise ValueError(
                f"No {n}th occurrence of weekday {weekday} in {year}-{month}"
            )

        return nth_occurrence

    def _parse_start_pattern(self, pattern: str, year: int) -> date | None:
        """
        Parse a start pattern and return the actual date.

        Args:
            pattern: Start pattern string
            year: Year for calculation

        Returns:
            Calculated start date
        """
        if pattern is None:
            return None

        # Monday = 0 in Python's weekday system
        monday = 0

        if pattern == "first_monday_january":
            return self._get_nth_weekday(year, 1, monday, 1)
        elif pattern == "second_monday_january":
            return self._get_nth_weekday(year, 1, monday, 2)
        elif pattern == "third_monday_january":
            return self._get_nth_weekday(year, 1, monday, 3)
        elif pattern == "first_monday_february":
            return self._get_nth_weekday(year, 2, monday, 1)
        elif pattern == "second_monday_february":
            return self._get_nth_weekday(year, 2, monday, 2)
        elif pattern == "third_monday_february":
            return self._get_nth_weekday(year, 2, monday, 3)
        elif pattern == "first_monday_march":
            return self._get_nth_weekday(year, 3, monday, 1)
        elif pattern == "second_monday_march":
            return self._get_nth_weekday(year, 3, monday, 2)
        elif pattern == "first_monday_april":
            return self._get_nth_weekday(year, 4, monday, 1)
        elif pattern == "second_monday_april":
            return self._get_nth_weekday(year, 4, monday, 2)
        elif pattern == "third_monday_april":
            return self._get_nth_weekday(year, 4, monday, 3)
        elif pattern == "first_monday_december_prev":
            return self._get_nth_weekday(year - 1, 12, monday, 1)
        else:
            raise ValueError(f"Unknown start pattern: {pattern}")

    def _calculate_end_date(self, start_date: date, duration_weeks: int) -> date | None:
        """
        Calculate the end date (Friday) based on start date and duration.

        Args:
            start_date: Session start date (should be a Monday)
            duration_weeks: Duration in weeks

        Returns:
            End date (Friday of the final week)
        """
        if start_date is None or duration_weeks == 0:
            return None

        # Friday = 4 in Python's weekday system
        friday = 4

        # Calculate the end of the session period
        end_period = start_date + timedelta(weeks=duration_weeks)

        # Find the Friday of that week
        days_to_friday = friday - end_period.weekday()
        if days_to_friday < 0:  # We're past Friday, go to previous Friday
            days_to_friday -= 7

        end_date = end_period + timedelta(days=days_to_friday)

        return end_date

    def get_session_dates(self, state_code: str, year: int) -> dict[str, Any]:
        """
        Get legislative session dates for a given state and year.

        Args:
            state_code (str): Two-letter state ISO2 code (e.g., 'CA', 'TX')
            year (int): The year to lookup

        Returns:
            Dict containing start_date, end_date, notes, and metadata

        Raises:
            ValueError: If state code is invalid or year is invalid
        """
        # Validate inputs
        state_code = state_code.upper().strip()
        if state_code not in self.legislative_data:
            raise ValueError(f"Invalid state code: {state_code}")

        if not isinstance(year, int) or year < 1900 or year > 2100:
            raise ValueError(f"Invalid year: {year}")

        state_info = self.legislative_data[state_code]
        is_odd_year = year % 2 == 1

        # Determine which session data to use
        session_key = "odd_year" if is_odd_year else "even_year"
        session_data = state_info[session_key]

        # Handle biennial states that only meet in odd years
        if state_info["biennial_odd_only"] and not is_odd_year:
            return {
                "state_code": state_code,
                "year": year,
                "start_date": None,
                "end_date": None,
                "session_type": "no_session",
                "notes": session_data["notes"],
                "full_time_legislature": state_info["full_time"],
                "biennial_odd_only": True,
                "duration_weeks": 0,
                "session_length_days": 0,
            }

        # Calculate actual dates
        start_date = self._parse_start_pattern(session_data["start_pattern"], year)
        end_date = self._calculate_end_date(start_date, session_data["duration_weeks"])

        if start_date is None or end_date is None:
            session_length_days = 0
        else:
            session_length_days = (end_date - start_date).days + 1

        return {
            "state_code": state_code,
            "year": year,
            "start_date": start_date,
            "end_date": end_date,
            "session_type": "regular" if start_date else "no_session",
            "notes": session_data["notes"],
            "full_time_legislature": state_info["full_time"],
            "biennial_odd_only": state_info["biennial_odd_only"],
            "duration_weeks": session_data["duration_weeks"],
            "session_length_days": session_length_days,
        }

    def get_all_states(self) -> list:
        """Return list of all supported state codes."""
        return sorted(self.legislative_data.keys())

    def is_full_time_legislature(self, state_code: str) -> bool:
        """Check if a state has a full-time legislature."""
        state_code = state_code.upper().strip()
        if state_code not in self.legislative_data:
            raise ValueError(f"Invalid state code: {state_code}")
        return self.legislative_data[state_code]["full_time"]

    def is_biennial_state(self, state_code: str) -> bool:
        """Check if a state only meets in odd years."""
        state_code = state_code.upper().strip()
        if state_code not in self.legislative_data:
            raise ValueError(f"Invalid state code: {state_code}")
        return self.legislative_data[state_code]["biennial_odd_only"]

    def is_in_session(self, state_code: str, check_date: date | None = None) -> bool:
        """
        Check if a state is currently in session.

        Args:
            state_code: Two-letter state code
            check_date: Date to check (defaults to today)

        Returns:
            True if the state is in session on the given date
        """
        check_date = check_date or date.today()
        year = check_date.year

        try:
            session = self.get_session_dates(state_code, year)
        except ValueError:
            return False

        if session["start_date"] is None or session["end_date"] is None:
            return False

        return session["start_date"] <= check_date <= session["end_date"]

    def get_active_states(self, check_date: date | None = None) -> list[str]:
        """
        Get list of states currently in session.

        Args:
            check_date: Date to check (defaults to today)

        Returns:
            List of state codes currently in session
        """
        check_date = check_date or date.today()
        active = []

        for state_code in self.get_all_states():
            if self.is_in_session(state_code, check_date):
                active.append(state_code)

        return active
