# This flask app is acting as a sort of proxy server to receive an API request and forward
# it to the Voatz API, but routed through the Digital Democracy Project EC2 instance
# which has a white-labeled IP address with Voatz. The API call must first use the /get_tokens
# endpoint to authenticate with the DDP Bearer token and the Voatz user credentials
# then receive the WS and CSRF tokens in response.  From there, a second API call can be made to
# /get_users to return the full list of Voatz users and attributes.  The /user_updates endpoint
# will check the Voatz API and Brevo API to determine if there have been any additions or
# departures of Voatz users compared to what is in the Brevo CRM, then only output the changes
# in the response body.

from flask import Flask, request, jsonify
import json
import requests
# system modules
import os
import time
# session with retry for Brevo API to handle rate limiting (HTTP 429)
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Define the token (saved as an environment variable)
API_BEARER_TOKEN = os.getenv("API_BEARER_TOKEN")

# requests session configured to retry GET/PUT on 429
BREVO_SESSION = requests.Session()
_brevo_retry = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429],
    allowed_methods=["GET", "PUT"],
)
BREVO_SESSION.mount("https://", HTTPAdapter(max_retries=_brevo_retry))
BREVO_SESSION.mount("http://", HTTPAdapter(max_retries=_brevo_retry))
# manual retry count for rate-limited Brevo API calls
MAX_RETRIES = 3
# rate-limit target (requests per hour) for Brevo API
RATE_LIMIT_RPH = int(os.getenv('BREVO_RATE_LIMIT_RPH', '36000'))
REQUEST_DELAY = 3600.0 / RATE_LIMIT_RPH

app = Flask(__name__)

LOGIN_URL = "https://vapi-vrb.nimsim.com/voatz/organizations/users/login"

LOGIN_HEADERS = {
    'Accept-Encoding': 'identity',
    'Content-Type': 'application/json',
    'Origin': 'http://vapi-vrb.nimsim.com'
}

@app.route('/get_tokens', methods=['POST'])
def get_tokens():
    # Validate Bearer token
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith("Bearer "):
        return jsonify({'status': 'error', 'message': 'Missing or malformed Authorization header'}), 401

    token = auth_header.split("Bearer ")[1]
    if token != API_BEARER_TOKEN:
        return jsonify({'status': 'error', 'message': 'Invalid Bearer token'}), 403

    # Continue with login logic
    data = request.get_json()
    email = data.get('emailAddress')
    password = data.get('password')
    organization_id = data.get('organizationid')

    if not email or not password or not organization_id:
        return jsonify({
            'status': 'error',
            'message': 'Missing one or more required fields: emailAddress, password, organizationid'
        }), 400

    login_payload = {
        "emailAddress": email,
        "password": password,
        "authData": [{"key": "organizationid", "value": str(organization_id)}]
    }

    login_response = requests.post(LOGIN_URL, headers=LOGIN_HEADERS, json=login_payload)

    if login_response.status_code == 200 and login_response.text.strip() == "OK":
        ws_token = login_response.cookies.get('WS') or login_response.headers.get('WS')
        csrf_token = login_response.cookies.get('Csrf-Token') or login_response.headers.get('Csrf-Token')

        if ws_token and csrf_token:
            return jsonify({
                'status': 'success',
                'WS': ws_token,
                'Csrf-Token': csrf_token
            }), 200
        else:
            return jsonify({'status': 'error', 'message': 'Tokens not found'}), 500
    else:
        return jsonify({
            'status': 'error',
            'message': 'Login failed',
            'status_code': login_response.status_code,
            'text': login_response.text
        }), 502

@app.route('/get_users', methods=['POST'])
def get_users():
    data = request.get_json()
    organization_id = int(data.get('organizationId'))
    ws_token = data.get('WS')
    csrf_token = data.get('Csrf-Token')
    mode = request.args.get('mode')

    if not organization_id or not ws_token or not csrf_token:
        return jsonify({"status": "error", "message": "Missing required fields."}), 400

    users_list_url = "https://vapi-vrb.nimsim.com/voatz/customers/delegate/signups/byorg"
    headers = {
        'Accept-Encoding': 'identity',
        'Content-Type': 'application/json',
        'Origin': 'http://vapi-vrb.nimsim.com',
        'WS': ws_token,
        'Csrf-Token': csrf_token,
        'Cookie': f"WS={ws_token}; Csrf-Token={csrf_token}"
    }

    users = []
    min_id = None

    while True:
        payload = {
            "organizationId": organization_id,
            "limit": 1000
        }
        if min_id:
            payload["minId"] = min_id

        response = requests.post(users_list_url, headers=headers, json=payload)
        if response.status_code != 200:
            return jsonify({
                "message": "Failed to retrieve users.",
                "status": "error",
                "code": response.status_code,
                "text": response.text
            }), response.status_code

        try:
            response_data = response.json()
        except Exception as e:
            return jsonify({
                "status": "error",
                "message": "Failed to parse JSON from Voatz response.",
                "details": response.text
            }), 500

        result = response_data.get("result", [])

        if not result:
            break

        users.extend(result)
        min_id = response_data.get("minId")

    if mode == 'diff_only':
        # Handle optional voatz_blacklist
        blacklist_raw = data.get("voatz_blacklist", [])
        if isinstance(blacklist_raw, str):
            blacklist = set(v.strip() for v in blacklist_raw.split(',') if v.strip())
        elif isinstance(blacklist_raw, list):
            blacklist = set(str(v).strip() for v in blacklist_raw)
        else:
            blacklist = set()

        voter_ids_from_api = []
        voter_details_by_id = {}

        def flatten_user(user):
            flattened = {k: v for k, v in user.items() if k != 'orgVerificationStatus'}
            kv_pairs = user.get("orgVerificationStatus", {}).get("keyValues", [])
            for pair in kv_pairs:
                key = pair.get("key")
                value = pair.get("value")
                if key and value is not None:
                    flattened[key] = value
            return flattened

        for user in users:
            kv = user.get("orgVerificationStatus", {}).get("keyValues", [])
            for pair in kv:
                if pair.get("key") == "Voter_Id":
                    voter_id = str(pair.get("value")).strip()
                    if voter_id not in blacklist:
                        voter_ids_from_api.append(voter_id)
                        voter_details_by_id[voter_id] = flatten_user(user)
                    break

        voter_ids_from_brevo = data.get("voter_ids", [])
        if isinstance(voter_ids_from_brevo, str):
            brevo_ids = [v.strip() for v in voter_ids_from_brevo.split(',') if v.strip()]
        elif isinstance(voter_ids_from_brevo, list):
            brevo_ids = [str(v).strip() for v in voter_ids_from_brevo]
        else:
            return jsonify({"status": "error", "message": "Invalid voter_ids format"}), 400

        api_set = set(voter_ids_from_api)
        brevo_set = set(brevo_ids)

        added_ids = api_set - brevo_set - blacklist
        removed_ids = brevo_set - api_set

        added_users = [voter_details_by_id[v_id] for v_id in added_ids]
        removed_users = [v_id for v_id in removed_ids]

        return jsonify({
            "status": "success",
            "diff_mode": True,
            "added_users": added_users,
            "removed_voter_ids": removed_users,
            "api_total": len(api_set),
            "brevo_total": len(brevo_set),
            "new_count": len(added_users),
            "removed_count": len(removed_users)
        }), 200

    else:
        return jsonify({"status": "success", "users": users}), 200

@app.route('/user_updates', methods=['POST'])
def compare_users():
    data = request.get_json()
    organization_id = data.get('organizationId')
    ws_token = data.get('WS')
    csrf_token = data.get('Csrf-Token')
    brevo_api_key = data.get('brevo_api_key')
    brevo_list_id = data.get('brevo_list_id')
    blacklist_raw = data.get('voatz_blacklist', [])

    if not all([organization_id, ws_token, csrf_token, brevo_api_key, brevo_list_id]):
        return jsonify({"status": "error", "message": "Missing required fields."}), 400

    # Normalize blacklist
    if isinstance(blacklist_raw, str):
        blacklist = set(v.strip() for v in blacklist_raw.split(',') if v.strip())
    elif isinstance(blacklist_raw, list):
        blacklist = set(str(v).strip() for v in blacklist_raw)
    else:
        blacklist = set()

    # Fetch voter list from Voatz
    users_list_url = "https://vapi-vrb.nimsim.com/voatz/customers/delegate/signups/byorg"
    headers_voatz = {
        'Accept-Encoding': 'identity',
        'Content-Type': 'application/json',
        'Origin': 'http://vapi-vrb.nimsim.com',
        'WS': ws_token,
        'Csrf-Token': csrf_token,
        'Cookie': f"WS={ws_token}; Csrf-Token={csrf_token}"
    }

    users = []
    min_id = None

    while True:
        payload = {"organizationId": int(organization_id), "limit": 1000}
        if min_id:
            payload["minId"] = min_id

        resp = requests.post(users_list_url, headers=headers_voatz, json=payload)
        if resp.status_code != 200:
            return jsonify({"status": "error", "message": "Voatz API failed", "text": resp.text}), resp.status_code

        result = resp.json().get("result", [])
        if not result:
            break
        users.extend(result)
        min_id = resp.json().get("minId")

    def flatten_user(user):
        flattened = {
            "Voter_Id": None,
            "firstName": None,
            "lastName": None,
            "emailAddress": user.get("email"),
            "phone": user.get("phone"),
            "precinct": None,
            "birthDate": None,
            "zip5": None,
            "timestamp": user.get("timestamp")
        }

        kv_pairs = user.get("orgVerificationStatus", {}).get("keyValues", [])
        for pair in kv_pairs:
            key = pair.get("key")
            value = pair.get("value")
            if not value:
                continue
            if key == "Voter_Id":
                flattened["Voter_Id"] = str(value).strip()
            elif key == "First_Name":
                flattened["firstName"] = str(value).strip()
            elif key == "Last_Name":
                flattened["lastName"] = str(value).strip()
            elif key == "Precinct":
                flattened["precinct"] = str(value).strip()
            elif key == "Birth_Date":
                flattened["birthDate"] = str(value).strip()
            elif key == "Zip5":
                flattened["zip5"] = str(value).strip()

        return flattened

    voter_ids_from_api = []
    voter_details_by_id = {}

    for user in users:
        kv = user.get("orgVerificationStatus", {}).get("keyValues", [])
        for pair in kv:
            if pair.get("key") == "Voter_Id":
                voter_id = str(pair.get("value")).strip()
                if voter_id not in blacklist:
                    voter_ids_from_api.append(voter_id)
                    voter_details_by_id[voter_id] = flatten_user(user)
                break

    api_set = set(voter_ids_from_api)

    # Fetch voter list from Brevo
    brevo_ids = []
    brevo_details_by_id = {}
    headers_brevo = {
        "Accept": "application/json",
        "api-key": brevo_api_key
    }
    offset = 0
    limit = 500
    base_url = f"https://api.brevo.com/v3/contacts/lists/{brevo_list_id}/contacts"

    while True:
        params = {"limit": limit, "offset": offset}
        brevo_resp = requests.get(base_url, headers=headers_brevo, params=params)
        if brevo_resp.status_code != 200:
            return jsonify({"status": "error", "message": "Brevo API failed", "text": brevo_resp.text}), brevo_resp.status_code

        brevo_data = brevo_resp.json()
        contacts = brevo_data.get("contacts", [])
        for contact in contacts:
            voter_id = contact.get("attributes", {}).get("VOTER_ID")
            if voter_id:
                voter_id_str = str(voter_id).strip()
                if voter_id_str and voter_id_str not in blacklist:
                    brevo_ids.append(voter_id_str)
                    brevo_details_by_id[voter_id_str] = {
                        "Voter_Id": voter_id_str,
                        "emailAddress": contact.get("email"),
                        "firstName": contact.get("attributes", {}).get("FIRSTNAME"),
                        "lastName": contact.get("attributes", {}).get("LASTNAME")
                    }

        if len(contacts) < limit:
            break
        offset += limit

    brevo_set = set(brevo_ids)

    added_ids = api_set - brevo_set - blacklist
    removed_ids = brevo_set - api_set - blacklist

    added_users = [voter_details_by_id[v_id] for v_id in added_ids if v_id in voter_details_by_id]
    removed_users = [brevo_details_by_id[v_id] for v_id in removed_ids if v_id in brevo_details_by_id]

    return jsonify({
        "status": "success",
        "diff_mode": True,
        "added_users": added_users,
        "removed_users": removed_users,
        "api_total": len(api_set),
        "brevo_total": len(brevo_set),
        "new_count": len(added_users),
        "removed_count": len(removed_users)
    }), 200

@app.route('/get_events', methods=['POST'])
def get_events():
    data = request.get_json()
    organization_id = data.get('organizationId')
    ws_token = data.get('WS')
    csrf_token = data.get('Csrf-Token')
    limit = data.get('limit')  # Optional
    min_ts = data.get('minTs')  # Optional

    if not organization_id or not ws_token or not csrf_token:
        return jsonify({"status": "error", "message": "Missing required fields: organizationId, WS, or Csrf-Token"}), 400

    url = "https://vapi-vrb.nimsim.com/voatz/events/listbyorganization/chrono"
    headers = {
        'Accept-Encoding': 'identity',
        'Content-Type': 'application/json',
        'Origin': 'http://vapi-vrb.nimsim.com',
        'WS': ws_token,
        'Csrf-Token': csrf_token,
        'Cookie': f"WS={ws_token}; Csrf-Token={csrf_token}"
    }

    payload = {
        "organizationId": organization_id
    }
    if limit:
        payload["limit"] = limit
    if min_ts:
        payload["minTs"] = min_ts

    response = requests.post(url, headers=headers, json=payload)

    if response.status_code != 200:
        return jsonify({
            "status": "error",
            "message": "Failed to fetch events",
            "code": response.status_code,
            "text": response.text
        }), response.status_code

    try:
        events_data = response.json()
    except Exception:
        return jsonify({
            "status": "error",
            "message": "Invalid JSON in response",
            "raw_response": response.text
        }), 500

    return jsonify({
        "status": "success",
        "events": events_data
    }), 200


@app.route('/create_event', methods=['POST'])
def create_event():
    data = request.get_json()
    organization_id = data.get('organizationId')
    ws_token = data.get('WS')
    csrf_token = data.get('Csrf-Token')
    if not organization_id or not ws_token or not csrf_token:
        return jsonify({"status": "error", "message": "Missing required fields: organizationId, WS, or Csrf-Token"}), 400

    url = "https://vapi-vrb.nimsim.com/voatz/events/create"
    headers = {
        'Accept-Encoding': 'identity',
        'Content-Type': 'application/json',
        'Origin': 'http://vapi-vrb.nimsim.com',
        'WS': ws_token,
        'Csrf-Token': csrf_token,
        'Cookie': f"WS={ws_token}; Csrf-Token={csrf_token}"
    }
    payload = data.copy()
    payload.pop('WS', None)
    payload.pop('Csrf-Token', None)
    payload.pop('organizationId', None)

    response = requests.post(url, headers=headers, json=payload)
    if response.status_code != 200:
        return jsonify({"status": "error", "message": "Failed to create event", "code": response.status_code, "text": response.text}), response.status_code

    try:
        result = response.json()
    except Exception:
        return jsonify({"status": "success", "raw_response": response.text}), 200

    return jsonify({"status": "success", "result": result}), 200

@app.route('/update_segment_attribute', methods=['POST'])
def update_segment_attribute():
	# Require bearer token for this proxy
	auth_header = request.headers.get('Authorization')
	if not auth_header or not auth_header.startswith('Bearer '):
		return jsonify({'status': 'error', 'message': 'Missing or malformed Authorization header'}), 401
	token = auth_header.split('Bearer ')[1]
	if token != API_BEARER_TOKEN:
		return jsonify({'status': 'error', 'message': 'Invalid Bearer token'}), 403

	data = request.get_json() or {}
	brevo_api_key = data.get('brevo_api_key')
	segment_id = data.get('segment_id')
	attr_name = data.get('attribute_name')
	attr_value = data.get('attribute_value')
	if not brevo_api_key or segment_id is None or not attr_name:
		return jsonify({'status': 'error', 'message': 'Missing required fields: brevo_api_key, segment_id, attribute_name'}), 400

	# Fetch contacts in segment (paginated)
	headers_brevo = {
		'Accept': 'application/json',
		'api-key': brevo_api_key
	}
	contacts = []
	offset = 0
	limit = 500
	while True:
		params = {'segmentId': int(segment_id), 'limit': limit, 'offset': offset}
		# retry GET on rate limit
		for attempt in range(MAX_RETRIES + 1):
			rep = BREVO_SESSION.get('https://api.brevo.com/v3/contacts', headers=headers_brevo, params=params)
			if rep.status_code == 429:
				time.sleep(REQUEST_DELAY)
				continue
			resp = rep
			break
		if resp.status_code != 200:
			return jsonify({'status': 'error', 'message': 'Failed to fetch contacts', 'code': resp.status_code, 'text': resp.text}), resp.status_code
		page = resp.json().get('contacts', [])
		if not page:
			break
		contacts.extend(page)
		offset += limit
		time.sleep(REQUEST_DELAY)

	# Update each contact's attribute
	success, failed = [], []
	for c in contacts:
		cid = c.get('id')
		if not cid:
			continue
		url = f'https://api.brevo.com/v3/contacts/{cid}?identifierType=contact_id'
		payload = {'attributes': {attr_name: attr_value}}
		# retry PUT on rate limit
		for attempt in range(MAX_RETRIES + 1):
			rep = BREVO_SESSION.put(url, headers={**headers_brevo, 'Content-Type': 'application/json'}, json=payload)
			if rep.status_code == 429:
				time.sleep(REQUEST_DELAY)
				continue
			r = rep
			break
		time.sleep(REQUEST_DELAY)
		if r.status_code == 200:
			success.append(cid)
		else:
			failed.append({'id': cid, 'code': r.status_code, 'text': r.text})

	return jsonify({
		'status': 'success',
		'total': len(contacts),
		'updated': len(success),
		'failures': failed
	}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
