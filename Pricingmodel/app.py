from flask import Flask, render_template, request, jsonify, send_file, flash, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import requests
import random
from io import BytesIO
from xhtml2pdf import pisa
from dotenv import load_dotenv
import os

load_dotenv()


app = Flask(__name__)
#app.secret_key = 'super-secret-key-2026'
app.secret_key = os.getenv('SECRET_KEY')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')


app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///quotations.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)


class Quotation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    charter_id = db.Column(db.String(50), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    pricing_model = db.Column(db.String(50))
    client = db.Column(db.String(200), nullable=True)         
    passengers = db.Column(db.Integer, nullable=True)
    start_location = db.Column(db.String(200))
    end_location = db.Column(db.String(200))
    trip_date = db.Column(db.String(20))
    start_time = db.Column(db.String(10))
    trip_type = db.Column(db.String(20))

    miles = db.Column(db.Float)
    hours = db.Column(db.Float)
    return_miles = db.Column(db.Float, nullable=True)
    return_hours = db.Column(db.Float, nullable=True)
    wait_time = db.Column(db.Float, nullable=True)

    one_way_price = db.Column(db.Float)
    return_price = db.Column(db.Float, nullable=True)
    total_price = db.Column(db.Float)

    base_rate_used = db.Column(db.Float)
    hour_rate_used = db.Column(db.Float)
    mile_rate = db.Column(db.Float)
    
    pickup_instructions = db.Column(db.Text, nullable=True)        
    destination_instructions = db.Column(db.Text, nullable=True)    


with app.app_context():
    db.create_all()


# ────────────────────────────────────────────────
#          FIXED PRICING PARAMETERS
# ────────────────────────────────────────────────
PRICING_MODELS = {
    'gowindstar': {
        'name': 'gowindstar',
        'base_rate': 500.00,
        'mile_flatrate':0.00,
        'hour_flatrate': 0.00
    },
    'windstar': {
        'name': 'windstar',
        'base_rate': 500.00,
        'mile_flatrate': 3.00,
        'hour_flatrate': 100.00
    }
}

#GOOGLE_API_KEY = 'AIzaSyAqRf0bx5hxH-CAVhR7KmqZgCilEzKQZsM'


def get_day_type(date_str: str) -> str:
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    # Monday=0 … Sunday=6; treat Fri/Sat/Sun as weekend
    return 'weekend' if date_obj.weekday() >= 4 else 'weekday'


def calculate_leg_price(miles: float, hours: float, model: dict) -> tuple:
    """
    Returns: (total_price: float, base: float, hour_rate: float)
    """
    mile_rate = model['mile_flatrate']          # 3.00 for both models
    pricing_model = model['name']
    day_type = model.get('day_type', 'weekday') # injected by caller

    if pricing_model == 'gowindstar':

        if day_type == 'weekday':
            if hours <= 4:
                base = 1000.00
            elif hours <= 5.5:
                base = 1400.00
            elif hours <= 7:
                base = 2133.50
            elif hours <= 10:
                base = 3000.00
            elif hours <= 14:
                base = 4100.00
            else:
                base = 4700.00

        elif day_type == 'weekend':
            if hours <= 4.5:
                base = 1100.00
            elif hours <= 5.5:
                base = 1600.00
            elif hours <= 7:
                base = 2400.00
            elif hours <= 10:
                base = 3500.00
            elif hours <= 14:
                base = 4400.00
            else:
                base = 4942.00

        else:
            raise ValueError("Invalid day_type")

        # Go Windstar: base bracket + mileage; no separate hourly charge
        hour_rate = 0.0
        mileage_cost = miles * mile_rate
        total = base + mileage_cost
        return round(total, 2), round(base, 2), round(hour_rate, 2)

    elif pricing_model == 'windstar':
        if hours > 48:
            base = 500.00
            hour_rate = 50.00
        elif hours > 24:
            base = 1200.00
            hour_rate = 100.00
        else:
            base = 1200.00
            hour_rate = 100.00

        mileage_cost = miles * mile_rate
        hourly_cost = hours * hour_rate
        total = base + mileage_cost + hourly_cost
        return round(total, 2), round(base, 2), round(hour_rate, 2)

    else:
        raise ValueError("Unknown pricing model")


def get_distance_matrix(origin: str, destination: str, departure_time: int = None) -> dict:
    url = 'https://maps.googleapis.com/maps/api/distancematrix/json'
    params = {
        'origins': origin,
        'destinations': destination,
        'mode': 'driving',
        'units': 'imperial',
        'key': GOOGLE_API_KEY
    }
    if departure_time:
        params['departure_time'] = departure_time

    try:
        response = requests.get(url, params=params, timeout=5)
        response.raise_for_status()
        data = response.json()

        if data['status'] != 'OK' or data['rows'][0]['elements'][0].get('status') != 'OK':
            raise ValueError(data.get('error_message', 'API error'))

        element = data['rows'][0]['elements'][0]
        miles = element['distance']['value'] / 1609.34
        hours = element['duration']['value'] / 3600.0
        if 'duration_in_traffic' in element:
            hours = element['duration_in_traffic']['value'] / 3600.0

        return {'miles': round(miles, 2), 'hours': round(hours, 2)}

    except Exception as e:
        raise ValueError(f"Failed to fetch distance: {str(e)}")


def generate_pdf(html_content):
    pdf_buffer = BytesIO()
    pisa_status = pisa.CreatePDF(
        src=html_content,
        dest=pdf_buffer,
        encoding='utf-8'
    )
    if pisa_status.err:
        raise ValueError("PDF generation failed")
    pdf_buffer.seek(0)
    return pdf_buffer


# ────────────────────────────────────────────────
#          ROUTES
# ────────────────────────────────────────────────

@app.route('/fetch_distance', methods=['POST'])
def fetch_distance():
    try:
        data = request.json
        start = data.get('start_location', '').strip()
        end = data.get('end_location', '').strip()
        trip_type = data.get('trip_type', 'oneway')
        date_str = data.get('date', '')
        start_time_str = data.get('start_time', '')

        if not start or not end:
            return jsonify({'error': 'Start and end locations required'}), 400

        departure_ts = None
        if date_str and start_time_str:
            try:
                dt = datetime.strptime(f"{date_str} {start_time_str}", "%Y-%m-%d %H:%M")
                departure_ts = int(dt.timestamp())
            except ValueError:
                pass

        out_data = get_distance_matrix(start, end, departure_ts)
        result = {'outbound': out_data}

        if trip_type == 'roundtrip':
            ret_data = get_distance_matrix(end, start, departure_ts)
            result['return'] = ret_data

        return jsonify(result)

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f"Unexpected error: {str(e)}"}), 500


@app.route('/', methods=['GET', 'POST'])
def price_calculator():
    result = None
    error = None

    if request.method == 'POST':
        try:
            miles = float(request.form.get('miles', 0))
            hours = float(request.form.get('hours', 0))
            trip_type = request.form.get('trip_type', 'oneway')
            pricing_model_key = request.form.get('pricing_model', 'gowindstar')

            if pricing_model_key not in PRICING_MODELS:
                raise ValueError("Invalid pricing model selected")

            # FIX: copy model dict so we don't mutate the global, then inject day_type
            model = dict(PRICING_MODELS[pricing_model_key])
            date_str = request.form['date']
            day_type = get_day_type(date_str)
            model['day_type'] = day_type

            start_time_str = request.form.get('start_time', '')
            start_location = request.form.get('start_location', '').strip()
            end_location = request.form.get('end_location', '').strip()

            wait_time = 0.0
            return_miles = 0.0
            return_hours = 0.0
            return_leg = None

            if trip_type == 'roundtrip':
                return_miles = float(request.form.get('return_miles', 0))
                return_hours = float(request.form.get('return_hours', 0))
                wait_time = float(request.form.get('wait_time', 0))

            if miles < 0 or hours < 0 or return_miles < 0 or return_hours < 0 or wait_time < 0:
                raise ValueError("Values cannot be negative")

            if not start_location or not end_location:
                raise ValueError("Please provide both start and end locations")

            if date_str and start_time_str:
                try:
                    datetime.strptime(f"{date_str} {start_time_str}", "%Y-%m-%d %H:%M")
                except ValueError:
                    raise ValueError("Invalid date or time format")

            # Calculate outbound leg
            one_way_price, one_way_base, one_way_hour_rate = calculate_leg_price(miles, hours, model)

            if trip_type == 'roundtrip':
                effective_return_hours = return_hours + wait_time
                return_leg_price, return_base, return_hour_rate = calculate_leg_price(
                    return_miles, effective_return_hours, model
                )
                total_price = one_way_price + return_leg_price
                return_leg = {
                    'miles': return_miles,
                    'hours': return_hours,
                    'wait_time': wait_time,
                    'effective_hours': effective_return_hours,
                    'price': return_leg_price,
                    'base_rate': return_base,
                    'hour_rate': return_hour_rate
                }
            else:
                total_price = one_way_price
                return_leg = None

            result = {
                'trip_type': trip_type,
                'pricing_model': model['name'],
                'client': request.form.get('client', '').strip(),
                'passengers': request.form.get('passengers', ''),
                'pickup_instructions': request.form.get('pickup_instructions', '').strip(),
                'destination_instructions': request.form.get('destination_instructions', '').strip(),
                'one_way': {
                    'miles': miles,
                    'hours': hours,
                    'price': one_way_price,
                    'base_rate': one_way_base,
                    'hour_rate': one_way_hour_rate
                },
                'return_leg': return_leg,
                'total_price': total_price,
                'date': date_str,
                'start_time': start_time_str,
                'start_location': start_location,
                'end_location': end_location,
                'pricing': {
                    'mile_rate': model['mile_flatrate'],
                }
            }

        except ValueError as e:
            error = str(e)
        except Exception as e:
            error = f"Unexpected error: {str(e)}"

    return render_template('index.html', result=result, error=error, google_api_key=GOOGLE_API_KEY)


@app.route('/save_quotation', methods=['POST'])
def save_quotation():
    try:
        q = Quotation(
            charter_id=f"ACS-{datetime.now().strftime('%Y%m%d')}-{random.randint(10000,99999)}",
            pricing_model=request.form['pricing_model'],
            passengers=request.form.get('passengers', 0),
            client=request.form.get('client', '').strip(),
            start_location=request.form['start_location'],
            end_location=request.form['end_location'],
            trip_date=request.form['date'],
            start_time=request.form['start_time'],
            trip_type=request.form['trip_type'],
            miles=float(request.form['miles']),
            hours=float(request.form['hours']),
            return_miles=float(request.form.get('return_miles', 0)) or None,
            return_hours=float(request.form.get('return_hours', 0)) or None,
            wait_time=float(request.form.get('wait_time', 0)) or None,
            one_way_price=float(request.form['one_way_price']),
            return_price=float(request.form.get('return_price', 0)) or None,
            total_price=float(request.form['total_price']),
            base_rate_used=float(request.form['base_rate']),
            hour_rate_used=float(request.form['hour_rate']),
            pickup_instructions=request.form.get('pickup_instructions', '').strip(),
            destination_instructions=request.form.get('destination_instructions', '').strip(),
            mile_rate=3.00
        )
        db.session.add(q)
        db.session.commit()
        return redirect(url_for('quotations_list'))
    except Exception as e:
        return f"<h2>Error saving quotation</h2><p>{str(e)}</p><a href='/'>Back to homepage</a>"


@app.route('/quotations')
def quotations_list():
    quotes = Quotation.query.order_by(Quotation.created_at.desc()).all()
    return render_template('quotations_list.html', quotations=quotes)


@app.route('/download/<charter_id>')
def download_quotation(charter_id):
    q = Quotation.query.filter_by(charter_id=charter_id).first_or_404()
    html = render_template('invoice_template.html', quotation=q)
    pdf = generate_pdf(html)
    return send_file(pdf, download_name=f"{q.charter_id}.pdf", as_attachment=True)


if __name__ == '__main__':
    app.run(debug=True)