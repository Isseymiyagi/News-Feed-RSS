import streamlit as st
import sqlite3
import folium
from streamlit_folium import st_folium
import json
from branca.element import Element
from branca.element import MacroElement
from jinja2 import Template

st.set_page_config(layout="wide", page_title="Global Intelligence Map")

st.title("🌍 World Update (Past 24 Hours)")
st.caption("Map updates dynamically. Shaded colors show briefing volume captured over the last 24 hours. Click any shaded nation to open its feed link.")

def get_rolling_24h_counts():
    """Queries database to extract metrics captured strictly in the last 24 hours."""
    conn = sqlite3.connect("news_vault.db")
    cursor = conn.cursor()
    # SQL query filtering records against a rolling 24-hour time differential limit
    cursor.execute("""
        SELECT country_code, COUNT(*) 
        FROM articles 
        WHERE timestamp >= datetime('now', '-1 day')
        GROUP BY country_code
    """)
    raw_data = cursor.fetchall()
    conn.close()
    
    # 2-letter to 3-letter fallback normalizer dictionary layer
    ISO_2_TO_3 = {'US': 'USA', 'GB': 'GBR', 'UK': 'GBR', 'FR': 'FRA', 'JP': 'JPN', 'BR': 'BRA', 'ET': 'ETH'}
    
    normalized_counts = {}
    for code, count in raw_data:
        if not code: continue
        code_upper = code.upper().strip()
        iso_3 = ISO_2_TO_3.get(code_upper, code_upper)
        normalized_counts[iso_3] = normalized_counts.get(iso_3, 0) + count
    return normalized_counts

country_metrics = get_rolling_24h_counts()

@st.cache_data
def load_geojson():
    with open("world_countries.json", "r", encoding="utf-8") as f:
        return json.load(f)

world_geojson = load_geojson()


NAME_TO_ISO = {
    "Afghanistan": "AFG", "Albania": "ALB", "Algeria": "DZA", "Andorra": "AND", "Angola": "AGO", "Antigua and Barbuda": "ATG", "Argentina": "ARG",
    "Armenia": "ARM", "Australia": "AUS", "Austria": "AUT", "Azerbaijan": "AZE", "Bahamas": "BHS", "Bahrain": "BHR", "Bangladesh": "BGD", "Barbados": "BRB", "Belarus": "BLR", "Belgium": "BEL", "Belize": "BLZ", "Benin": "BEN", "Bhutan": "BTN", 
    "Bolivia": "BOL", "Bosnia and Herzegovina": "BIH", "Botswana": "BWA", "Brazil": "BRA", "Brunei": "BRN", "Bulgaria": "BGR", "Burkina Faso": "BFA", "Burundi": "BDI", "Cabo Verde": "CPV", "Cambodia": "KHM", "Cameroon": "CMR", "Canada": "CAN", "Central African Republic": "CAF",
    "Chad": "TCD", "Chile": "CHL", "China": "CHN", "Colombia": "COL", "Comoros": "COM", "Costa Rica": "CRI", "Côte d'Ivoire": "CIV", "Croatia": "HRV", "Cuba": "CUB", "Cyprus": "CYP", "Czechia": "CZE", "Democratic Republic of the Congo": "COD", "Denmark": "DNK",
    "Djibouti": "DJI", "Dominica": "DMA", "Dominican Republic": "DOM", "Ecuador": "ECU", "Egypt": "EGY", "El Salvador": "SLV", "Equatorial Guinea": "GNQ", "Eritrea": "ERI", "Estonia": "EST", "Eswatini": "SWZ", "Ethiopia": "ETH", "Fiji": "FJI", "Finland": "FIN", "France": "FRA", "Gabon": "GAB",
    "Gambia": "GMB", "Georgia": "GEO", "Germany": "DEU", "Ghana": "GHA", "Greece": "GRC", "Grenada": "GRD", "Guatemala": "GTM", "Guinea": "GIN", "Guinea-Bissau": "GNB", "Guyana": "GUY", "Haiti": "HTI", "Honduras": "HND", "Hungary": "HUN", "Iceland": "ISL", "India": "IND",
    "Indonesia": "IDN", "Iran": "IRN", "Iraq": "IRQ", "Ireland": "IRL", "Israel": "ISR", "Italy": "ITA", "Jamaica": "JAM", "Japan": "JPN", "Jordan": "JOR", "Kazakhstan": "KAZ", "Kenya": "KEN", "Kiribati": "KIR", "Kuwait": "KWT", "Kyrgyzstan": "KGZ", "Laos": "LAO",
    "Latvia": "LVA", "Lebanon": "LBN", "Lesotho": "LSO", "Liberia": "LBR", "Libya": "LBY", "Liechtenstein": "LIE", "Lithuania": "LTU", "Luxembourg": "LUX", "Madagascar": "MDG", "Malawi": "MWI", "Malaysia": "MYS", "Maldives": "MDV", "Mali": "MLI", "Malta": "MLT", 
    "Marshall Islands": "MHL", "Mauritania": "MRT", "Mauritius": "MUS", "Mexico": "MEX", "Micronesia": "FSM", "Moldova": "MDA", "Monaco": "MCO", "Mongolia": "MNG", "Montenegro": "MNE", "Morocco": "MAR", "Mozambique": "MOZ", "Myanmar": "MMR", "Namibia": "NAM",
    "Nauru": "NRU", "Nepal": "NPL", "Netherlands": "NLD", "New Zealand": "NZL", "Nicaragua": "NIC", "Niger": "NER", "Nigeria": "NGA", "North Korea": "PRK", "North Macedonia": "MKD", "Norway": "NOR", "Oman": "OMN", "Pakistan": "PAK", "Palau": "PLW", "Panama": "PAN",
    "Papua New Guinea": "PNG", "Paraguay": "PRY", "Peru": "PER", "Philippines": "PHL", "Poland": "POL", "Portugal": "PRT", "Qatar": "QAT", "Republic of the Congo": "COG", "Romania": "ROU", "Russia": "RUS", "Rwanda": "RWA", "Saint Kitts and Nevis": "KNA",
    "Saint Lucia": "LCA", "Saint Vincent and the Grenadines": "VCT", "Samoa": "WSM", "San Marino": "SMR", "São Tomé and Príncipe": "STP", "Saudi Arabia": "SAU", "Senegal": "SEN", "Serbia": "SRB", "Seychelles": "SYC", "Sierra Leone": "SLE", "Singapore": "SGP",
    "Slovakia": "SVK", "Slovenia": "SVN", "Solomon Islands": "SLB", "Somalia": "SOM", "South Africa": "ZAF", "South Korea": "KOR", "South Sudan": "SSD", "Spain": "ESP", "Sri Lanka": "LKA", "Sudan": "SDN", "Suriname": "SUR", "Sweden": "SWE", "Switzerland": "CHE",
    "Syria": "SYR", "Taiwan": "TWN", "Tajikistan": "TJK", "Tanzania": "TZA", "Thailand": "THA", "Timor-Leste": "TLS", "Togo": "TGO", "Tonga": "TON", "Trinidad and Tobago": "TTO", "Tunisia": "TUN", "Türkiye": "TUR", "Turkmenistan": "TKM", "Tuvalu": "TUV", 
    "Uganda": "UGA", "Ukraine": "UKR", "United Arab Emirates": "ARE", "United Kingdom": "GBR", "USA": "USA", "Uruguay": "URY", "Uzbekistan": "UZB", "Vanuatu": "VUT", "Venezuela": "VEN", "Vietnam": "VNM", "Yemen": "YEM", "Zambia": "ZMB", "Zimbabwe": "ZWE",
}


COUNTRY_ANCHORS = {
    # --- NORTH & CENTRAL AMERICA ---
    "USA": {"lat": 39.8283, "lon": -98.5795},    # Strict geographic center of mainland US
    "CAN": {"lat": 56.1304, "lon": -106.3468},  # Shifted south from the Arctic islands
    "MEX": {"lat": 23.6345, "lon": -102.5528},
    "GTM": {"lat": 15.7835, "lon": -90.2308},
    "BLZ": {"lat": 17.1899, "lon": -88.4976},
    "SLV": {"lat": 13.7942, "lon": -88.8965},
    "HND": {"lat": 15.2000, "lon": -86.2419},
    "NIC": {"lat": 12.8654, "lon": -85.2072},
    "CRI": {"lat": 9.7489, "lon": -83.7534},
    "PAN": {"lat": 8.5380, "lon": -80.7821},
    "CUB": {"lat": 21.5218, "lon": -77.7812},
    "HTI": {"lat": 18.9712, "lon": -72.2852},
    "DOM": {"lat": 18.7357, "lon": -70.1627},
    "JAM": {"lat": 18.1096, "lon": -77.2975},
    "BHS": {"lat": 25.0343, "lon": -77.3963},

    # --- SOUTH AMERICA ---
    "BRA": {"lat": -14.2350, "lon": -51.9253},
    "ARG": {"lat": -38.4161, "lon": -63.6167},
    "COL": {"lat": 4.5709, "lon": -74.2973},
    "PER": {"lat": -9.1900, "lon": -75.0152},
    "VEN": {"lat": 6.4238, "lon": -66.5897},
    "CHL": {"lat": -35.6751, "lon": -71.5430}, # Shifted to center of narrow strip
    "ECU": {"lat": -1.8312, "lon": -78.1834},
    "BOL": {"lat": -16.2902, "lon": -63.5887},
    "PRY": {"lat": -23.4425, "lon": -58.4438},
    "URY": {"lat": -32.5228, "lon": -55.7658},
    "GUY": {"lat": 4.8604, "lon": -58.9302},
    "SUR": {"lat": 3.9193, "lon": -56.0278},

    # --- EUROPE (Spaced to avoid overlapping) ---
    "GBR": {"lat": 54.5000, "lon": -3.5000},   # Centered on UK mainland
    "FRA": {"lat": 46.6033, "lon": 2.4468},    # Mainland France only
    "DEU": {"lat": 51.1657, "lon": 10.4515},
    "ITA": {"lat": 42.5000, "lon": 12.5000},
    "ESP": {"lat": 40.4637, "lon": -3.7492},
    "PRT": {"lat": 39.3999, "lon": -8.2245},
    "IRL": {"lat": 53.4129, "lon": -8.2439},
    "BEL": {"lat": 50.5039, "lon": 4.4699},
    "NLD": {"lat": 52.1326, "lon": 5.2913},
    "CHE": {"lat": 46.8182, "lon": 8.2275},
    "AUT": {"lat": 47.5162, "lon": 14.5501},
    "POL": {"lat": 51.9194, "lon": 19.1451},
    "CZE": {"lat": 49.8175, "lon": 15.4730},
    "SVK": {"lat": 48.6690, "lon": 19.6990},
    "HUN": {"lat": 47.1625, "lon": 19.5033},
    "ROU": {"lat": 45.9432, "lon": 24.9668},
    "BGR": {"lat": 42.7339, "lon": 25.4858},
    "GRC": {"lat": 39.0742, "lon": 21.8243},
    "UKR": {"lat": 48.3794, "lon": 31.1656},
    "BLR": {"lat": 53.7098, "lon": 27.9534},
    "MDA": {"lat": 47.4116, "lon": 28.3699},
    "DNK": {"lat": 56.2639, "lon": 9.5018},
    "NOR": {"lat": 61.5000, "lon": 8.5000},    # Kept south to stay on landmass
    "SWE": {"lat": 60.1282, "lon": 18.6435},
    "FIN": {"lat": 61.9241, "lon": 25.7482},
    "EST": {"lat": 58.5953, "lon": 25.0136},
    "LVA": {"lat": 56.8796, "lon": 24.6032},
    "LTU": {"lat": 55.1694, "lon": 23.8813},
    "SRB": {"lat": 44.0165, "lon": 21.0059},
    "HRV": {"lat": 45.1000, "lon": 15.2000},
    "BIH": {"lat": 43.9159, "lon": 17.6791},
    "SVN": {"lat": 46.1512, "lon": 14.9955},
    "MKD": {"lat": 41.6086, "lon": 21.7453},
    "ALB": {"lat": 41.1533, "lon": 20.1683},
    "MNE": {"lat": 42.7087, "lon": 19.3744},
    "XKX": {"lat": 42.6026, "lon": 20.9030},

    # --- MIDDLE EAST & CENTRAL ASIA ---
    "TUR": {"lat": 38.9637, "lon": 35.2433},
    "ISR": {"lat": 31.0461, "lon": 34.8516},
    "PSE": {"lat": 31.9522, "lon": 35.2332},
    "JOR": {"lat": 31.2400, "lon": 36.5100},
    "LBN": {"lat": 33.8547, "lon": 35.8623},
    "SYR": {"lat": 34.8021, "lon": 38.9968},
    "IRQ": {"lat": 33.2232, "lon": 43.6793},
    "IRN": {"lat": 32.4279, "lon": 53.6880},
    "SAU": {"lat": 23.8859, "lon": 45.0792},
    "YEM": {"lat": 15.5527, "lon": 48.5164},
    "OMN": {"lat": 21.4735, "lon": 55.9754},
    "ARE": {"lat": 23.4241, "lon": 53.8478},
    "QAT": {"lat": 25.3548, "lon": 51.1839},
    "KWT": {"lat": 29.3117, "lon": 47.4818},
    "BHR": {"lat": 26.0667, "lon": 50.5577},
    "KAZ": {"lat": 48.0196, "lon": 66.9237},
    "UZB": {"lat": 41.3775, "lon": 64.5853},
    "TKM": {"lat": 38.9697, "lon": 59.5563},
    "KGZ": {"lat": 41.2044, "lon": 74.7661},
    "TJK": {"lat": 38.8610, "lon": 71.2761},

    # --- ASIA ---
    "RUS": {"lat": 55.7558, "lon": 42.0000},   # Drastically shifted west into Europe-Russia zone to stop text trailing into Siberia
    "CHN": {"lat": 35.8617, "lon": 104.1954},
    "IND": {"lat": 20.5937, "lon": 78.9629},
    "JPN": {"lat": 36.2048, "lon": 138.2529},
    "KOR": {"lat": 35.9078, "lon": 127.7669},
    "PRK": {"lat": 40.3399, "lon": 127.5101},
    "TWN": {"lat": 23.6978, "lon": 120.9605},
    "MNG": {"lat": 46.8625, "lon": 103.8467},
    "PAK": {"lat": 30.3753, "lon": 69.3451},
    "AFG": {"lat": 33.9391, "lon": 67.7099},
    "NPL": {"lat": 28.3949, "lon": 84.1240},
    "BGD": {"lat": 23.6850, "lon": 90.3563},
    "LKA": {"lat": 7.8731, "lon": 80.7718},
    "MMR": {"lat": 21.9162, "lon": 95.9560},
    "THA": {"lat": 15.8700, "lon": 100.9925},
    "VNM": {"lat": 14.0583, "lon": 108.2772},
    "KHM": {"lat": 12.5657, "lon": 104.9910},
    "LAO": {"lat": 19.8562, "lon": 102.4955},
    "MYS": {"lat": 4.2105, "lon": 101.9758},   # Split set on mainland Malaysia
    "SGP": {"lat": 1.3521, "lon": 103.8198},
    "IDN": {"lat": -0.7893, "lon": 113.9213},  # Centered safely inside Borneo
    "PHL": {"lat": 12.8797, "lon": 121.7740},

    # --- AFRICA ---
    "EGY": {"lat": 26.8206, "lon": 30.8025},
    "LBY": {"lat": 26.3351, "lon": 17.2283},
    "TUN": {"lat": 33.8869, "lon": 9.5375},
    "DZA": {"lat": 28.0339, "lon": 1.6596},
    "MAR": {"lat": 31.7917, "lon": -7.0926},
    "SDN": {"lat": 12.8628, "lon": 30.2176},
    "SSD": {"lat": 6.8770, "lon": 31.3070},
    "ETH": {"lat": 9.1450, "lon": 40.4897},
    "SOM": {"lat": 5.1521, "lon": 46.1996},
    "KEN": {"lat": -0.0236, "lon": 37.9062},
    "UGA": {"lat": 1.3733, "lon": 32.2903},
    "TZA": {"lat": -6.3690, "lon": 34.8888},
    "COD": {"lat": -4.0383, "lon": 21.7587},  # DR Congo center
    "COG": {"lat": -0.2280, "lon": 15.8277},   # Congo Republic center
    "CAF": {"lat": 6.6111, "lon": 20.9394},
    "TCD": {"lat": 15.4542, "lon": 18.7322},
    "NER": {"lat": 17.6078, "lon": 8.0817},
    "MLI": {"lat": 17.5707, "lon": -3.9962},
    "MRT": {"lat": 21.0079, "lon": -10.9408},
    "SEN": {"lat": 14.4974, "lon": -14.4524},
    "NGA": {"lat": 9.0820, "lon": 8.6753},
    "GHA": {"lat": 7.9465, "lon": -1.0232},
    "CIV": {"lat": 7.5399, "lon": -5.5471},    # Ivory Coast
    "CMR": {"lat": 7.3697, "lon": 12.3547},
    "AGO": {"lat": -11.2027, "lon": 17.8739},
    "ZAF": {"lat": -30.5595, "lon": 22.9375},  # South Africa
    "ZWE": {"lat": -19.0154, "lon": 29.1549},
    "ZMB": {"lat": -13.1339, "lon": 27.8493},
    "MOZ": {"lat": -18.6657, "lon": 35.5296},
    "MDG": {"lat": -18.7669, "lon": 46.8691},  # Madagascar

    # --- OCEANIA ---
    "AUS": {"lat": -25.2744, "lon": 133.7751},
    "NZL": {"lat": -40.9006, "lon": 174.8860},
    "PNG": {"lat": -6.3149, "lon": 143.9555},
    "FJI": {"lat": -17.7134, "lon": 178.0650}
}

# CATEGORIZE COUNTRIES BY GEOGRAPHIC FOOTPRINT SCALE
# Group names into tiers so the CSS engine knows which zoom level triggers them

LABEL_TIERS = {
    # LARGE — Zoom 2+ (12)
    "Algeria": "label-large", "Argentina": "label-large", "Australia": "label-large", "Brazil": "label-large", "Canada": "label-large", "China": "label-large", "Democratic Republic of the Congo": "label-large", "India": "label-large",
    "Kazakhstan": "label-large", "Russia": "label-large", "Saudi Arabia": "label-large", "USA": "label-large",

    # MEDIUM — Zoom 3+ (72)
    "Afghanistan": "label-medium", "Angola": "label-medium", "Belarus": "label-medium", "Bolivia": "label-medium", "Botswana": "label-medium", "Burkina Faso": "label-medium", "Cameroon": "label-medium", "Central African Republic": "label-medium",
    "Chad": "label-medium", "Chile": "label-medium", "Colombia": "label-medium", "Côte d'Ivoire": "label-medium", "Ecuador": "label-medium", "Egypt": "label-medium", "Ethiopia": "label-medium", "Finland": "label-medium",
    "France": "label-medium", "Gabon": "label-medium", "Germany": "label-medium", "Ghana": "label-medium", "Guinea": "label-medium", "Guyana": "label-medium", "Indonesia": "label-medium", "Iran": "label-medium",
    "Iraq": "label-medium", "Italy": "label-medium", "Japan": "label-medium", "Kenya": "label-medium", "Laos": "label-medium", "Libya": "label-medium", "Madagascar": "label-medium", "Malaysia": "label-medium",
    "Mali": "label-medium", "Mauritania": "label-medium", "Mexico": "label-medium", "Mongolia": "label-medium", "Morocco": "label-medium", "Mozambique": "label-medium", "Myanmar": "label-medium", "Namibia": "label-medium",
    "New Zealand": "label-medium", "Niger": "label-medium", "Nigeria": "label-medium", "Norway": "label-medium", "Oman": "label-medium", "Pakistan": "label-medium", "Papua New Guinea": "label-medium", "Paraguay": "label-medium",
    "Peru": "label-medium", "Philippines": "label-medium", "Poland": "label-medium", "Republic of the Congo": "label-medium", "Romania": "label-medium", "Somalia": "label-medium", "South Africa": "label-medium", "South Sudan": "label-medium",
    "Spain": "label-medium", "Sudan": "label-medium", "Sweden": "label-medium", "Tanzania": "label-medium", "Thailand": "label-medium", "Turkmenistan": "label-medium", "Türkiye": "label-medium", "Uganda": "label-medium", "Ukraine": "label-medium",
    "United Kingdom": "label-medium", "Uzbekistan": "label-medium", "Venezuela": "label-medium", "Vietnam": "label-medium", "Yemen": "label-medium", "Zambia": "label-medium", "Zimbabwe": "label-medium",

    # SMALL — Zoom 4+ (110)
    "Albania": "label-small", "Andorra": "label-small", "Antigua and Barbuda": "label-small", "Armenia": "label-small", "Austria": "label-small", "Azerbaijan": "label-small", "Bahamas": "label-small", "Bahrain": "label-small", "Bangladesh": "label-small", "Barbados": "label-small",
    "Belgium": "label-small", "Belize": "label-small", "Benin": "label-small", "Bhutan": "label-small", "Bosnia and Herzegovina": "label-small", "Brunei": "label-small", "Bulgaria": "label-small", "Burundi": "label-small", "Cabo Verde": "label-small", "Cambodia": "label-small", "Comoros": "label-small",
    "Costa Rica": "label-small", "Croatia": "label-small", "Cuba": "label-small", "Cyprus": "label-small", "Czechia": "label-small", "Denmark": "label-small", "Djibouti": "label-small", "Dominica": "label-small", "Dominican Republic": "label-small", "El Salvador": "label-small",
    "Equatorial Guinea": "label-small", "Eritrea": "label-small", "Estonia": "label-small", "Eswatini": "label-small", "Fiji": "label-small", "Gambia": "label-small", "Georgia": "label-small", "Greece": "label-small", "Grenada": "label-small", "Guatemala": "label-small", "Guinea-Bissau": "label-small",
    "Haiti": "label-small", "Honduras": "label-small", "Hungary": "label-small", "Iceland": "label-small", "Ireland": "label-small", "Israel": "label-small", "Jamaica": "label-small", "Jordan": "label-small", "Kiribati": "label-small", "Kuwait": "label-small", "Kyrgyzstan": "label-small",
    "Latvia": "label-small", "Lebanon": "label-small", "Lesotho": "label-small", "Liberia": "label-small", "Liechtenstein": "label-small", "Lithuania": "label-small", "Luxembourg": "label-small", "Malawi": "label-small", "Maldives": "label-small", "Malta": "label-small",
    "Marshall Islands": "label-small", "Mauritius": "label-small", "Micronesia": "label-small", "Moldova": "label-small", "Monaco": "label-small", "Montenegro": "label-small", "Nauru": "label-small", "Nepal": "label-small", "Netherlands": "label-small", "Nicaragua": "label-small", "North Korea": "label-small",
    "North Macedonia": "label-small", "Palau": "label-small", "Panama": "label-small", "Portugal": "label-small", "Qatar": "label-small", "Rwanda": "label-small", "Saint Kitts and Nevis": "label-small", "Saint Lucia": "label-small", "Saint Vincent and the Grenadines": "label-small", "Samoa": "label-small",
    "San Marino": "label-small", "Senegal": "label-small", "Serbia": "label-small", "Seychelles": "label-small", "Sierra Leone": "label-small", "Singapore": "label-small", "Slovakia": "label-small", "Slovenia": "label-small", "Solomon Islands": "label-small", "South Korea": "label-small",
    "Sri Lanka": "label-small", "Suriname": "label-small", "Switzerland": "label-small", "Syria": "label-small", "São Tomé and Príncipe": "label-small", "Taiwan": "label-small", "Tajikistan": "label-small", "Timor-Leste": "label-small", "Togo": "label-small", "Tonga": "label-small",
    "Trinidad and Tobago": "label-small", "Tunisia": "label-small", "Tuvalu": "label-small", "United Arab Emirates": "label-small", "Uruguay": "label-small", "Vanuatu": "label-small",
}

assert len(LABEL_TIERS) == 194





COUNTRY_LABEL_CSS = """
<style>
.country-label {
    font-family: 'Segoe UI', Helvetica, Arial, sans-serif;
    font-weight: 600;
    color: #2C3E50;
    text-shadow:
        -1px -1px 0 #FFF,
         1px -1px 0 #FFF,
        -1px  1px 0 #FFF,
         1px  1px 0 #FFF;
    white-space: nowrap;
    text-align: center;
    transform: translate(-50%, -50%);
    display: none;
    pointer-events: none;
    width: max-content;
}

/* Zoom-appropriate label sizes */
.label-large { font-size: 12px; }
.label-medium { font-size: 10px; }
.label-small { font-size: 9px; }
</style>
"""




# Inject active counts directly into GeoJSON layer properties for native Popups
for feature in world_geojson['features']:
    iso_3 = feature['id']
    country_name = feature['properties']['name']
    count = country_metrics.get(iso_3, 0)
    feature['properties']['news_count'] = count
    
    # REQUIREMENT 1: Embedding HTML popup directly into the boundary layer definition.
    # This completely eliminates cluttering markers!
    if count > 0:
        html_popup = f"""
        <div style="font-family: Arial, sans-serif; font-size: 12px; width: 160px;">
            <h4 style="margin:0 0 5px 0;">{country_name}</h4>
            <p style="margin:0 0 10px 0;"><b>{count} updates today.</b></p>
           <a href="/news_feed?country={iso_3}" target="_blank" style="color: #ff4b4b; font-weight: bold; text-decoration: none;"> ➡️ Open News Feed </a>
        </div>
        """
        feature['properties']['popup_html'] = html_popup
    else:
        feature['properties']['popup_html'] = f"<h4>{country_name}</h4><p>No new updates in past 24 hours.</p>"

# 1. Define the CARTO Voyager configuration base layer (Using no labels so they don't clash)
VOYAGER_NO_LABELS = "https://{s}://{z}/{x}/{y}{r}.png"
VOYAGER_ATTR = '&copy; <a href="https://openstreetmap.org">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'

m = folium.Map(
    location=[20.0, 0.0], 
    zoom_start=2,
    tiles=VOYAGER_NO_LABELS,   # Clean layout without baseline full-text labels
    attr=VOYAGER_ATTR
)

if 'features' in world_geojson:
    for feature in world_geojson['features']:
                # Try finding the country identifier across common GeoJSON property schemas
        full_name = feature['properties'].get('name', 'Unknown')
        tier_class = LABEL_TIERS.get(full_name, "label-small")
         # Cross-map the name using translation keys to find its coordinates
        iso_lookup = NAME_TO_ISO.get(full_name, full_name[:3].upper())
        
        # 1. Look up our clean manual anchor map coordinates first
        if iso_lookup in COUNTRY_ANCHORS:
            lat = COUNTRY_ANCHORS[iso_lookup]['lat']
            lon = COUNTRY_ANCHORS[iso_lookup]['lon']
        else:
            # Fallback calculation logic for countries not in our manual anchor list
            try:
                geometry = feature['geometry']
                if geometry['type'] == 'Polygon':
                    # Grab coordinates safely
                    pts = geometry['coordinates'][0]
                elif geometry['type'] == 'MultiPolygon':
                    # Pick the largest polygon fragment array to calculate from
                    pts = max(geometry['coordinates'], key=lambda p: len(p[0]))[0]
                else:
                    continue
                lat = sum(p[1] for p in pts) / len(pts)
                lon = sum(p[0] for p in pts) / len(pts)
            except Exception:
                continue

        # 2. Build a highly readable HTML tag with a maximum layout boundary width
        # Create a perfectly proportioned HTML tag for the 3-letter abbreviation code
        
        folium.Marker(
            location=[lat, lon],
            icon=folium.DivIcon(
                icon_size=(0, 0),
                icon_anchor=(0, 0),
                html=f"""
                <div class="country-label {tier_class}">
                    {full_name}
                </div>
                """
         )
    ).add_to(m)




# REQUIREMENT 2: Color coding scale updates dynamically based on active 24h ranges
folium.Choropleth(
    geo_data=world_geojson,
    name="choropleth",
    data=country_metrics,
    key_on="feature.id",
    fill_color="YlOrRd",
    fill_opacity=0.6,
    line_opacity=0.2,
    legend_name="Intelligence Updates (Past 24 Hours Only)",
    nan_fill_color="#eeeeee"
).add_to(m)


# Add the interactive GeoJSON popup layer onto the primary canvas map
folium.GeoJson(
    world_geojson,
    style_function=lambda x: {'fillOpacity': 0.0, 'color': 'transparent'},
    tooltip=folium.GeoJsonTooltip(fields=['name', 'news_count'], aliases=['Country:', '24h Briefs:']),
    popup=folium.GeoJsonPopup(fields=['popup_html'], labels=False)
).add_to(m)




class CountryLabelController(MacroElement):
    def __init__(self):
        super().__init__()
        self._name = "CountryLabelController"

        self._template = Template("""
        {% macro script(this, kwargs) %}

        var countryMap = {{ this._parent.get_name() }};

        function updateCountryLabels() {
            var zoom = countryMap.getZoom();

            var labels = countryMap.getContainer()
                .querySelectorAll('.country-label');

            labels.forEach(function(label) {
                var isLarge = label.classList.contains('label-large');
                var isMedium = label.classList.contains('label-medium');
                var isSmall = label.classList.contains('label-small');

                var visible =
                    (isLarge && zoom >= 2) ||
                    (isMedium && zoom >= 3) ||
                    (isSmall && zoom >= 4);

                label.style.display = visible ? 'block' : 'none';

                if (isLarge) {
                    label.style.fontSize = zoom >= 5 ? '14px' : '11px';
                } else if (isMedium) {
                    label.style.fontSize = zoom >= 5 ? '12px' : '10px';
                } else if (isSmall) {
                    label.style.fontSize = zoom >= 5 ? '10px' : '9px';
                }
            });
        }

        // Initial display
        updateCountryLabels();

        // Refresh labels when zoom changes
        countryMap.on('zoomend', updateCountryLabels);

        {% endmacro %}
        """)


# Inject label styling into Folium
m.get_root().header.add_child(
    Element(COUNTRY_LABEL_CSS)
)

# Attach controller after markers have been added
m.add_child(CountryLabelController())



st_folium(m, width="100%", height=600)
