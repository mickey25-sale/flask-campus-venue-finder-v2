const VENUES_CSV_URL =
    'https://docs.google.com/spreadsheets/d/e/2PACX-1vROiTq-W_OdNM2ZoWur6yJFHOOYaMBxMxis4cybVBH6YPeNdpRzjpe2GGjHxk_ziAOTs4iGF7k3Ajpz/pub?gid=0&single=true&output=csv';
const REFRESH_INTERVAL_MS = 30_000;

const LEAFLET_ASSET_PATH = 'https://unpkg.com/leaflet@1.9.4/dist/images/';
L.Icon.Default.mergeOptions({
    imagePath: LEAFLET_ASSET_PATH,
    iconRetinaUrl: 'marker-icon-2x.png',
    iconUrl: 'marker-icon.png',
    shadowUrl: 'marker-shadow.png'
});

const mapElement = document.getElementById('map');
const map = mapElement._campusVenueMap ||
    L.map(mapElement, { maxZoom: 19 }).setView([0.50396, 34.12548], 15);
mapElement._campusVenueMap = map;

const markerGroup = map._campusVenueMarkerGroup || L.markerClusterGroup({
    showCoverageOnHover: false,
    spiderfyOnMaxZoom: true
}).addTo(map);
map._campusVenueMarkerGroup = markerGroup;
map._campusVenueTileLayer || (map._campusVenueTileLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'
}).addTo(map));

const venueList = document.getElementById('venue-list');
const searchInput = document.getElementById('search-input');
const statusFilter = document.getElementById('status-filter');
const markers = new Map();
let venues = [];

function parseCsv(text) {
    const rows = [];
    let row = [];
    let value = '';
    let quoted = false;

    for (let index = 0; index < text.length; index += 1) {
        const character = text[index];

        if (quoted) {
            if (character === '"' && text[index + 1] === '"') {
                value += '"';
                index += 1;
            } else if (character === '"') {
                quoted = false;
            } else {
                value += character;
            }
        } else if (character === '"' && value.length === 0) {
            quoted = true;
        } else if (character === ',') {
            row.push(value);
            value = '';
        } else if (character === '\n') {
            row.push(value.replace(/\r$/, ''));
            if (row.some(cell => cell.trim())) rows.push(row);
            row = [];
            value = '';
        } else {
            value += character;
        }
    }

    row.push(value.replace(/\r$/, ''));
    if (row.some(cell => cell.trim())) rows.push(row);
    if (quoted) throw new Error('The venue CSV contains an unclosed quoted field.');

    const [headerRow, ...dataRows] = rows;
    if (!headerRow) throw new Error('The venue CSV is empty.');

    const headers = headerRow.map(header => header.trim().toLowerCase());
    const requiredHeaders = [
        'venue_name', 'location', 'status', 'updated_at', 'latitude', 'longitude'
    ];
    const missingHeader = requiredHeaders.find(header => !headers.includes(header));
    if (missingHeader) throw new Error(`The venue CSV is missing the ${missingHeader} column.`);

    return dataRows.map(cells => Object.fromEntries(
        headers.map((header, index) => [header, (cells[index] || '').trim()])
    ));
}

function normalizeStatus(status) {
    const normalized = (status || '').trim().toLowerCase();
    if (normalized === 'free') return 'available';
    return ['available', 'occupied', 'maintenance'].includes(normalized)
        ? normalized
        : 'unknown';
}

function coordinatesFor(venue) {
    if (!venue || venue.latitude == null || venue.longitude == null) {
        return null;
    }

    const latitudeValue = String(venue.latitude).trim();
    const longitudeValue = String(venue.longitude).trim();
    if (!latitudeValue || !longitudeValue) {
        return null;
    }

    const latitude = Number(latitudeValue);
    const longitude = Number(longitudeValue);
    if (
        !Number.isFinite(latitude) || !Number.isFinite(longitude) ||
        Math.abs(latitude) > 90 || Math.abs(longitude) > 180
    ) {
        return null;
    }
    return [latitude, longitude];
}

function formatUpdatedAt(value) {
    if (!value) return 'Update time unavailable';
    const date = new Date(value);
    return Number.isNaN(date.getTime())
        ? value
        : new Intl.DateTimeFormat(undefined, {
            dateStyle: 'medium',
            timeStyle: 'short'
        }).format(date);
}

function makeElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
}

function createVenueCard(venue, index) {
    const status = normalizeStatus(venue.status);
    const card = makeElement('article', 'venue-card');
    card.tabIndex = 0;
    card.setAttribute('role', 'button');
    card.setAttribute('aria-label', `Show ${venue.venue_name || 'venue'} on map`);
    card.addEventListener('click', () => focusVenue(card, index));
    card.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            focusVenue(card, index);
        }
    });

    card.append(
        makeElement('h3', '', venue.venue_name || 'Unnamed venue'),
        makeElement('p', 'venue-location', venue.location || 'Location not specified')
    );

    const footer = makeElement('div', 'venue-footer');
    footer.append(
        makeElement('span', `status ${status}`, status.toUpperCase()),
        makeElement('span', 'updated', `Updated ${formatUpdatedAt(venue.updated_at)}`)
    );
    card.append(footer);
    return card;
}

function focusVenue(card, index) {
    document.querySelectorAll('.venue-card').forEach(item => {
        item.classList.remove('selected');
    });
    card.classList.add('selected');
    const marker = markers.get(index);
    if (marker) {
        markerGroup.zoomToShowLayer(marker, () => marker.openPopup());
    }
}

function createPopup(venue) {
    const popup = makeElement('div');
    const name = makeElement('strong', '', venue.venue_name || 'Unnamed venue');
    const location = makeElement('div', '', venue.location || 'Location not specified');
    const status = makeElement('div', '', `Status: ${normalizeStatus(venue.status).toUpperCase()}`);
    popup.append(name, location, status);
    return popup;
}

function updateMap() {
    markerGroup.clearLayers();
    markers.clear();

    venues.forEach((venue, index) => {
        const coordinates = coordinatesFor(venue);
        if (!coordinates) return;

        const marker = L.marker(coordinates, {
            icon: L.divIcon({
                className: 'venue-marker-wrapper',
                html: `<span class="venue-map-marker ${statusClassFor(venue.status)}" aria-hidden="true"></span>`,
                iconSize: [23, 23],
                iconAnchor: [11, 11],
                popupAnchor: [0, -11]
            })
        });
        marker.bindPopup(createPopup(venue));
        markerGroup.addLayer(marker);
        markers.set(index, marker);
    });

    if (markers.size) {
        map.fitBounds(markerGroup.getBounds(), {
            padding: [28, 28],
            maxZoom: 17
        });
    }
}

function statusClassFor(status) {
    const className = normalizeStatus(status);
    return /^[a-z0-9_-]+$/.test(className) ? className : 'unknown';
}

function renderVenues() {
    const query = searchInput.value.trim().toLowerCase();
    const selectedStatus = statusFilter.value;
    const visibleVenues = venues
        .map((venue, index) => ({ venue, index }))
        .filter(({ venue }) => {
            const matchesSearch = [
                venue.venue_name, venue.location, venue.status
            ].join(' ').toLowerCase().includes(query);
            const matchesStatus = selectedStatus === 'all' ||
                normalizeStatus(venue.status) === selectedStatus;
            return matchesSearch && matchesStatus;
        });

    venueList.replaceChildren();
    if (!visibleVenues.length) {
        venueList.append(makeElement('p', 'message', 'No venues match your search.'));
        return;
    }

    visibleVenues.forEach(({ venue, index }) => {
        venueList.append(createVenueCard(venue, index));
    });
}

function updateSummary() {
    document.getElementById('total-count').textContent = venues.length;
    document.getElementById('available-count').textContent =
        venues.filter(venue => normalizeStatus(venue.status) === 'available').length;
    document.getElementById('occupied-count').textContent =
        venues.filter(venue => normalizeStatus(venue.status) === 'occupied').length;
    document.getElementById('maintenance-count').textContent =
        venues.filter(venue => normalizeStatus(venue.status) === 'maintenance').length;
}

async function loadVenues() {
    const status = document.getElementById('refresh-status');
    const error = document.getElementById('page-error');
    try {
        const response = await fetch(VENUES_CSV_URL, { cache: 'no-store' });
        if (!response.ok) throw new Error(`Venue sheet returned HTTP ${response.status}.`);
        venues = parseCsv(await response.text());
        updateSummary();
        renderVenues();
        updateMap();
        status.textContent = 'Availability loaded';
        document.getElementById('last-updated').textContent =
            `Checked ${new Intl.DateTimeFormat(undefined, { timeStyle: 'short' }).format(new Date())}`;
        error.hidden = true;
    } catch (loadError) {
        status.textContent = 'Unable to load venue data';
        error.textContent = `${loadError.message} Please check your connection and try again.`;
        error.hidden = false;
        if (!venues.length) venueList.replaceChildren();
    }
}

if ('ResizeObserver' in window) {
    new ResizeObserver(() => map.invalidateSize()).observe(mapElement);
} else {
    window.addEventListener('resize', () => map.invalidateSize());
}
window.requestAnimationFrame(() => map.invalidateSize());

searchInput.addEventListener('input', renderVenues);
statusFilter.addEventListener('change', renderVenues);
loadVenues();
window.setInterval(loadVenues, REFRESH_INTERVAL_MS);
