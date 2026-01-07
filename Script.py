import os
import json
import base64
import logging
from flask import Flask
import pandas as pd
from shapely.geometry import Point, Polygon
import mysql.connector
import gspread
from gspread_dataframe import set_with_dataframe
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

# ---------------- Load Environment Variables ----------------
load_dotenv()

DB_HOST = os.environ.get("DB_HOST")
DB_USER = os.environ.get("DB_USER")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
DB_NAME = os.environ.get("DB_NAME")
GOOGLE_CREDS_BASE64 = os.environ.get("GOOGLE_CREDS_BASE64")
GOOGLE_SHEET_KEY = os.environ.get("GOOGLE_SHEET_KEY")

# ---------------- Logging Setup ----------------
logging.getLogger('werkzeug').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------- Flask App ----------------
app = Flask(__name__)

# ---------------- Helper: Compute Zone ----------------
def get_zone(lat, lng, zones):
    if pd.notna(lat) and pd.notna(lng):
        point = Point(float(lng), float(lat))
        for z in zones:
            if z["polygon"].contains(point):
                return z["area"]
    return None

# ---------------- Load Zones from Database ----------------
def load_zones(cursor):
    cursor.execute(
        "SELECT id, area, polygon FROM plb_city_area_polygons "
        "WHERE id NOT IN (3,4,5,6,10,17,18,9,19,20,1,11,21,22)"
    )
    zones_raw = cursor.fetchall()
    zones = []
    for z in zones_raw:
        try:
            pts = json.loads(z["polygon"])
            coords = [(float(p["lng"]), float(p["lat"])) for p in pts if p.get("lat") and p.get("lng")]
            poly = Polygon(coords)
            if not poly.is_valid:
                poly = poly.buffer(0)
            zones.append({"area": z["area"], "polygon": poly})
        except Exception as e:
            logger.error(f"Polygon load error for zone {z.get('id', 'unknown')} → {e}")
    logger.info(f"Loaded {len(zones)} zones from DB.")
    return zones

# ---------------- Route ----------------
@app.route("/update-sheet", methods=["GET", "POST"])
def update_sheet():
    try:
        logger.info("Starting sheet update...")

        # ---------------- Google Sheet Auth ----------------
        creds_json = base64.b64decode(GOOGLE_CREDS_BASE64)
        creds_info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(
            creds_info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"
            ]
        )
        client = gspread.authorize(creds)
        sheet = client.open_by_key(GOOGLE_SHEET_KEY).worksheet("Dump Data")

        # ---------------- Connect to MySQL ----------------
        db = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        cursor = db.cursor(dictionary=True)

        # ---------------- Load Zones ----------------
        zones = load_zones(cursor)

        # ---------------- Execute SQL ----------------
        # Note: Adjust the joins/fields if needed, simplified for readability
        sql =""" select plb_history_bookings.id,
        plb_history_bookings.booking_date,
        plb_history_bookings.booking_time,
        plb_history_bookings.created_at,
        CONCAT(
        LPAD(HOUR(plb_history_bookings.booking_time), 2, '0'), ':',
        LPAD(FLOOR(MINUTE(plb_history_bookings.booking_time) / 30) * 30, 2, '0'),
        ' - ',
        LPAD(HOUR(plb_history_bookings.booking_time + INTERVAL 30 MINUTE), 2, '0'), ':',
        LPAD(FLOOR(MINUTE(plb_history_bookings.booking_time + INTERVAL 30 MINUTE) / 30) * 30, 2, '0')
    ) AS slot,
          c.mobile_number,
          case
          when plb_history_bookings.family_member_id = 0 then c.gender
          else plb_customer_family_members.gender
          end as customer_gender,
          case
          when plb_history_bookings.family_member_id = 0 then c.dob
          else plb_customer_family_members.dob
          end as customer_dob,
        plb_history_bookings.booking_by,
        concat(plb_manages.first_name,' ',plb_manages.last_name) as Created_by_name,
        case when plb_history_bookings.booking_admin_type = 1 then 'P1 - Curelo New'
         when plb_history_bookings.booking_admin_type = 2 then 'P2 - Curelo Repeat'
         when plb_history_bookings.booking_admin_type = 3 then 'L1 - Lab New'
         when plb_history_bookings.booking_admin_type = 4 then 'L2 - Lab Repeat'
         when plb_history_bookings.booking_admin_type = 5 then 'C - Corporate Lead'
        end as Customer_Type,
        case
        when plb_history_bookings.booking_tracking_id = 1 then 'Order Placed'
        when plb_history_bookings.booking_tracking_id = 2 then 'Phlebotomist Assigned'
        when plb_history_bookings.booking_tracking_id = 3 then 'Phlebotomist On The Way'
        when plb_history_bookings.booking_tracking_id = 4 then 'Phlebotomist Reached at Destination'
        when plb_history_bookings.booking_tracking_id = 5 then 'Phlebotomist Collection Received'
        when plb_history_bookings.booking_tracking_id = 6 then 'Phlebotomist Sample Submitted'
        when plb_history_bookings.booking_tracking_id = 7 then 'Reports Preparing'
        when plb_history_bookings.booking_tracking_id = 8 then 'Reports Submitted'
        when plb_history_bookings.booking_tracking_id = 9 then 'Order Completed'
        end as Booking_Stage,
        plb_history_bookings.status,
        plb_history_bookings.booking_status,                           
        plb_history_bookings.promocode,
        case
           when plb_history_bookings.booking_by ='customer' and plb_promocodes.promocode_category is null then 'Organic Lead'
           when plb_history_bookings.booking_by ='lab' and plb_history_bookings.total_admin_commission = plb_history_bookings.fixed_admin_commission and plb_promocodes.promocode_category is null then 'Lab Lead'
           when plb_history_bookings.booking_by = 'admin' and plb_history_bookings.total_admin_commission = plb_history_bookings.fixed_admin_commission and plb_promocodes.promocode_category is null then 'Lab Lead by Admin' 
           when plb_history_bookings.booking_by ='admin' and plb_promocodes.promocode_category is null then 'Organic Lead by Admin'
           else plb_promocode_categories.name
          end as Channel,
        plb_labs.name AS Lab_Name,
        plb_cities.name AS city_name,
        group_concat(distinct case 
          when plb_lab_tests.title is null then plb_tests_and_packages_masters.name
          else plb_lab_tests.title
          end SEPARATOR ', ') as tests,
          group_concat(distinct case 
          when plb_lab_tests.title is null then plb_tests_and_packages_masters.type
          else plb_lab_tests.type
          end SEPARATOR ', ') as tests_type,
        concat(plb_phlebos.first_name,' ',plb_phlebos.last_name) as phlebo_name,
          plb_customer_addresses.pincode,
          plb_customer_addresses.latitude,
          plb_customer_addresses.longitude, 
          plb_customer_addresses.id AS address_id,                                     
        plb_history_bookings.total_actual_amount,
        plb_history_bookings.discount_amount,
        (plb_history_bookings.total_actual_amount-plb_history_bookings.discount_amount) as lab_mrp,
          plb_history_bookings.promocode_discount_amount,
          plb_history_bookings.redeem_coin,
          plb_history_bookings.total_paid_amount,
          plb_history_bookings.total_admin_commission,
          plb_history_bookings.total_lab_earning                     
          FROM plb_history_bookings
        LEFT Join plb_history_booking_tests on plb_history_bookings.id = plb_history_booking_tests.booking_id
        LEFT JOIN plb_promocodes ON plb_history_bookings.promocode = plb_promocodes.promocode
        LEFT JOIN plb_promocode_categories ON plb_promocodes.promocode_category = plb_promocode_categories.id
        LEFT JOIN plb_phlebos ON plb_phlebos.id = plb_history_bookings.phlebo_id
        LEFT JOIN plb_cities ON plb_cities.id = plb_history_bookings.city_id
        LEFT JOIN plb_customers AS c ON plb_history_bookings.customer_id = c.id 
        left join plb_customer_family_members on plb_history_bookings.family_member_id = plb_customer_family_members.id
        LEFT JOIN plb_customer_addresses ON plb_customer_addresses.id = plb_history_bookings.address_id
        LEFT JOIN plb_labs ON plb_labs.id = plb_history_bookings.lab_id
        LEFT JOIN plb_labs_branches ON plb_history_bookings.lab_branch_id = plb_labs_branches.id
        LEFT JOIN plb_lab_tests ON plb_lab_tests.id = plb_history_booking_tests.test_id
        left join plb_manages on plb_manages.id = plb_history_bookings.booking_by_id
        LEFT JOIN plb_tests_and_packages_masters on plb_lab_tests.test_and_package_id = plb_tests_and_packages_masters.id
        where  plb_history_bookings.booking_date BETWEEN CURDATE() AND DATE_ADD(CURDATE(), INTERVAL 1 DAY)
        group by plb_history_bookings.id
        union all
        select plb_bookings.id,
        plb_bookings.booking_date, 
        plb_bookings.booking_time,                      
        plb_bookings.created_at,
        CONCAT(
        LPAD(HOUR(plb_bookings.booking_time), 2, '0'), ':',
        LPAD(FLOOR(MINUTE(plb_bookings.booking_time) / 30) * 30, 2, '0'),
        ' - ',
        LPAD(HOUR(plb_bookings.booking_time + INTERVAL 30 MINUTE), 2, '0'), ':',
        LPAD(FLOOR(MINUTE(plb_bookings.booking_time + INTERVAL 30 MINUTE) / 30) * 30, 2, '0')
    ) AS slot,
          c.mobile_number,
          case
          when plb_bookings.family_member_id = 0 then c.gender
          else plb_customer_family_members.gender
          end as customer_gender,
          case
          when plb_bookings.family_member_id = 0 then c.dob
          else plb_customer_family_members.dob
          end as customer_dob,
        plb_bookings.booking_by,
        concat(plb_manages.first_name,' ',plb_manages.last_name) as Created_by_name,
        case when plb_bookings.booking_admin_type = 1 then 'P1 - Curelo New'
         when plb_bookings.booking_admin_type = 2 then 'P2 - Curelo Repeat'
         when plb_bookings.booking_admin_type = 3 then 'L1 - Lab New'
         when plb_bookings.booking_admin_type = 4 then 'L2 - Lab Repeat'
         when plb_bookings.booking_admin_type = 5 then 'C - Corporate Lead'
        end as Customer_Type,
        case
        when plb_bookings.booking_tracking_id = 1 then 'Order Placed'
        when plb_bookings.booking_tracking_id = 2 then 'Phlebotomist Assigned'
        when plb_bookings.booking_tracking_id = 3 then 'Phlebotomist On The Way'
        when plb_bookings.booking_tracking_id = 4 then 'Phlebotomist Reached at Destination'
        when plb_bookings.booking_tracking_id = 5 then 'Phlebotomist Collection Received'
        when plb_bookings.booking_tracking_id = 6 then 'Phlebotomist Sample Submitted'
        when plb_bookings.booking_tracking_id = 7 then 'Reports Preparing'
        when plb_bookings.booking_tracking_id = 8 then 'Reports Submitted'
        when plb_bookings.booking_tracking_id = 9 then 'Order Completed'
        end as Booking_Stage,
        plb_bookings.status,
        plb_bookings.booking_status,                      
        plb_bookings.promocode,
        case
           when plb_bookings.booking_by ='customer' and plb_promocodes.promocode_category is null then 'Organic Lead'
           when plb_bookings.booking_by ='lab' and plb_bookings.total_admin_commission = plb_bookings.fixed_admin_commission and plb_promocodes.promocode_category is null then 'Lab Lead'
           when plb_bookings.booking_by = 'admin' and plb_bookings.total_admin_commission = plb_bookings.fixed_admin_commission and plb_promocodes.promocode_category is null then 'Lab Lead by Admin' 
           when plb_bookings.booking_by ='admin' and plb_promocodes.promocode_category is null then 'Organic Lead by Admin'
           else plb_promocode_categories.name
          end as Channel,
        plb_labs.name AS Lab_Name,
        plb_cities.name AS city_name,
        group_concat(distinct case 
          when plb_lab_tests.title is null then plb_tests_and_packages_masters.name
          else plb_lab_tests.title
          end SEPARATOR ', ') as tests,
          group_concat(distinct case 
          when plb_lab_tests.title is null then plb_tests_and_packages_masters.type
          else plb_lab_tests.type
          end SEPARATOR ', ') as tests_type,
        concat(plb_phlebos.first_name,' ',plb_phlebos.last_name) as phlebo_name,
          plb_customer_addresses.pincode,
          plb_customer_addresses.latitude,
          plb_customer_addresses.longitude, 
          plb_customer_addresses.id AS address_id,           
        plb_bookings.total_actual_amount,
        plb_bookings.discount_amount,
        (plb_bookings.total_actual_amount-plb_bookings.discount_amount) as lab_mrp,
          plb_bookings.promocode_discount_amount,
          plb_bookings.redeem_coin,
          plb_bookings.total_paid_amount,
          plb_bookings.total_admin_commission,
          plb_bookings.total_lab_earning                   
          FROM plb_bookings
        LEFT Join plb_booking_tests on plb_bookings.id = plb_booking_tests.booking_id
        LEFT JOIN plb_promocodes ON plb_bookings.promocode = plb_promocodes.promocode
        LEFT JOIN plb_promocode_categories ON plb_promocodes.promocode_category = plb_promocode_categories.id
        LEFT JOIN plb_phlebos ON plb_phlebos.id = plb_bookings.phlebo_id
        LEFT JOIN plb_cities ON plb_cities.id = plb_bookings.city_id
        LEFT JOIN plb_customers AS c ON plb_bookings.customer_id = c.id 
        left join plb_customer_family_members on plb_bookings.family_member_id = plb_customer_family_members.id
        LEFT JOIN plb_customer_addresses ON plb_customer_addresses.id = plb_bookings.address_id
        LEFT JOIN plb_labs ON plb_labs.id = plb_bookings.lab_id
        LEFT JOIN plb_labs_branches ON plb_bookings.lab_branch_id = plb_labs_branches.id
        LEFT JOIN plb_lab_tests ON plb_lab_tests.id = plb_booking_tests.test_id
        left join plb_manages on plb_manages.id = plb_bookings.booking_by_id
        LEFT JOIN plb_tests_and_packages_masters on plb_lab_tests.test_and_package_id = plb_tests_and_packages_masters.id
        where  plb_bookings.booking_date BETWEEN CURDATE() AND DATE_ADD(CURDATE(), INTERVAL 1 DAY)
        group by plb_bookings.id
        order by created_at """

        cursor.execute(sql)
        rows = cursor.fetchall()
        df = pd.DataFrame(rows)

        if not df.empty:
            # ---------------- Compute Zones ----------------
            df["zone"] = df.apply(lambda r: get_zone(r.get("latitude"), r.get("longitude"), zones), axis=1)
            df = df.fillna("").astype(str)
            sheet.clear()
            set_with_dataframe(sheet, df)
            logger.info(f"Sheet updated successfully with {len(df)} rows.")
        else:
            logger.info("No bookings found for today.")

        return "OK", 200

    except Exception as e:
        logger.exception("Error updating sheet")
        return str(e), 500

# ---------------- Run Flask ----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
