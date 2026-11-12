const mapElement = document.getElementById('map');
const map = mapElement._campusVenueMap ||
    L.map(mapElement, { maxZoom: 19 }).setView([0.50396, 34.12548], 15);
mapElement._campusVenueMap = map;

const venueMarkerGroup = map._campusVenueMarkerGroup || L.markerClusterGroup({
    showCoverageOnHover: false,
    spiderfyOnMaxZoom: true,
    disableClusteringAtZoom: 19
}).addTo(map);
map._campusVenueMarkerGroup = venueMarkerGroup;

map._campusVenueTileLayer || (map._campusVenueTileLayer = L.tileLayer(
    'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    {
        maxZoom: 19,
        attribution: '&copy; OpenStreetMap contributors'
    }
).addTo(map));

// Each item contains its Leaflet marker and venue data.
const venueMarkers = [];

// Safely display text supplied by the spreadsheet.
function escapeMapText(value) {
    const element = document.createElement('span');
    element.textContent = value || 'Not specified';
    return element.innerHTML;
}

function escapeHTML(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, character => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
    })[character]);
}

function formatUpdatedAt(value) {
    if (!value) return 'Not specified';
    if (!/^\d{4}-\d{2}-\d{2}T/.test(value)) return value;

    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;

    return new Intl.DateTimeFormat(undefined, {
        dateStyle: 'medium',
        timeStyle: 'short'
    }).format(date);
}

function showAdminNotice(message, isError) {
    const notice = document.getElementById('adminNotice');
    if (!notice) return;
    notice.textContent = message;
    notice.classList.toggle('error', Boolean(isError));
    notice.hidden = false;
}

async function sendAdminRequest(url, method, payload) {
    const response = await fetch(url, {
        method,
        cache: 'no-store',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': csrfToken
        },
        body: JSON.stringify(payload)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(data.error || `Request failed (${response.status}).`);
    }
    return data;
}

function formVenueData(form) {
    const data = new FormData(form);
    return {
        venue_name: data.get('venue_name'),
        location: data.get('location'),
        status: data.get('status'),
        latitude: data.get('latitude'),
        longitude: data.get('longitude')
    };
}

async function submitAdminForm(form, url, method, successMessage) {
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
        const data = await sendAdminRequest(url, method, formVenueData(form));
        showAdminNotice(data.message || successMessage, false);
        if (form.id === 'createVenueForm') form.reset();
        else form.hidden = true;
        if (!await loadVenueMarkers()) {
            showAdminNotice(
                'The Sheet was updated, but venue data could not be refreshed. Reload the page or check the connection.',
                true
            );
        }
    } catch (error) {
        showAdminNotice(error.message || 'The venue change could not be saved.', true);
    } finally {
        button.disabled = false;
    }
}

const venueList = document.getElementById('venueList');
const statusFilter = document.getElementById('statusFilter');
const appMain = document.getElementById('appMain');
const isAdmin = appMain.dataset.isAdmin === 'true';
const csrfToken = appMain.dataset.csrfToken;
let venuesData = [];
let venueStatuses = [];
let hasFitVenueBounds = false;
let refreshInProgress = false;

function normalizedVenueStatus(status) {
    const normalized = String(status || '').trim().toUpperCase();
    if (normalized === 'FREE') return 'AVAILABLE';
    return venueStatuses.includes(normalized) ? normalized : 'UNKNOWN';
}

function statusClassFor(status) {
    return normalizedVenueStatus(status).toLowerCase();
}

function configureStatusFilter(options) {
    const currentValue = statusFilter.value;
    statusFilter.replaceChildren(new Option('All statuses', 'All'));
    options.forEach(status => {
        statusFilter.add(new Option(status, status));
    });
    statusFilter.value = options.includes(currentValue) ? currentValue : 'All';
}

function focusVenueOnMap(card) {
    document.querySelectorAll('#venueList .venue').forEach(item => {
        item.classList.remove('selected');
    });

    card.classList.add('selected');
    const selected = venueMarkers.find(item =>
        String(item.venue.id) === card.dataset.venueId
    );

    if (selected) {
        const target = selected.marker.getLatLng();
        const targetZoom = Math.min(
            map.getMaxZoom(),
            Math.max(map.getZoom(), 19)
        );
        if (
            map.getZoom() === targetZoom &&
            map.getCenter().equals(target)
        ) {
            selected.marker.openPopup();
            return;
        }

        map.setView(target, targetZoom, { animate: false });
        selected.marker.openPopup();
    }
}

function renderVenueCards(venues) {
    if (venues.length === 0) {
        venueList.innerHTML = '<p class="empty-state">No venues were found in the published Google Sheet.</p>';
        return;
    }

    venueList.innerHTML = venues.map(venue => {
        const status = normalizedVenueStatus(venue.status);
        const statusLabel = status === 'UNKNOWN' ? 'Unknown' : status;
        const venueName = escapeMapText(venue.venue_name || 'Unnamed venue');
        const location = escapeMapText(venue.location || 'Not specified');
        const updatedAt = escapeMapText(formatUpdatedAt(venue.updated_at));
        const adminControls = isAdmin ? `
            <div class="admin-card-actions">
                <button type="button" data-edit-venue>Edit</button>
                <button type="button" data-delete-venue>Delete</button>
            </div>
            <form class="admin-edit-form" data-venue-edit hidden>
                <label>Venue name
                    <input name="venue_name" maxlength="200" value="${escapeHTML(venue.venue_name)}" required>
                </label>
                <label>Location
                    <input name="location" maxlength="300" value="${escapeHTML(venue.location)}">
                </label>
                <label>Status
                    <select name="status" required>${venueStatuses.map(option =>
                        `<option value="${escapeHTML(option)}"${option === status ? ' selected' : ''}>${escapeHTML(option)}</option>`
                    ).join('')}</select>
                </label>
                <label>Latitude
                    <input name="latitude" inputmode="decimal" value="${escapeHTML(venue.latitude)}">
                </label>
                <label>Longitude
                    <input name="longitude" inputmode="decimal" value="${escapeHTML(venue.longitude)}">
                </label>
                <div class="admin-card-actions">
                    <button type="submit">Save changes</button>
                    <button type="button" data-cancel-edit>Cancel</button>
                </div>
            </form>` : '';

        return `<article class="venue" data-venue-id="${Number(venue.id)}" tabindex="0" aria-label="Show venue on map">
            <h3>${venueName}</h3>
            <p class="venue-location">${location}</p>
            <div class="venue-meta">
                <span class="venue-status ${statusClassFor(status)}">${escapeMapText(statusLabel)}</span>
                <p class="venue-updated">Updated ${updatedAt}</p>
            </div>
            ${adminControls}
        </article>`;
    }).join('');
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
        !Number.isFinite(latitude) ||
        !Number.isFinite(longitude) ||
        Math.abs(latitude) > 90 ||
        Math.abs(longitude) > 180
    ) {
        return null;
    }

    return [latitude, longitude];
}

function markerIconFor(status) {
    return L.divIcon({
        className: 'venue-marker-wrapper',
        html: `<span class="venue-map-marker ${statusClassFor(status)}" aria-hidden="true"></span>`,
        iconSize: [23, 23],
        iconAnchor: [11, 11],
        popupAnchor: [0, -11]
    });
}

function markerPopupFor(venue) {
    const status = normalizedVenueStatus(venue.status);
    const statusLabel = status === 'UNKNOWN' ? 'Unknown' : status;
    return '<b>' + escapeMapText(venue.venue_name) + '</b><br>' +
        'Location: ' + escapeMapText(venue.location) + '<br>' +
        'Status: ' + escapeMapText(statusLabel);
}

function createVenueMarker(venue, coordinates) {
    const marker = L.marker(coordinates, { icon: markerIconFor(venue.status) });
    marker.bindPopup(markerPopupFor(venue));
    return { marker, venue };
}

function updateVenueCounts() {
    const counts = Object.fromEntries(venueStatuses.map(status => [status, 0]));
    venuesData.forEach(venue => {
        const status = normalizedVenueStatus(venue.status);
        if (Object.hasOwn(counts, status)) counts[status] += 1;
    });

    document.getElementById('totalCount').textContent = venuesData.length;
    venueStatuses.forEach(status => {
        const countElement = document.getElementById(`${status.toLowerCase()}Count`);
        if (countElement) countElement.textContent = counts[status];
    });
}

function reconcileVenueMarkers(venues) {
    const previousMarkers = new Map(
        venueMarkers.map(item => [String(item.venue.id), item])
    );
    const nextMarkers = [];

    venues.forEach(venue => {
        const coordinates = coordinatesFor(venue);
        const key = String(venue.id);
        const existing = previousMarkers.get(key);
        if (!coordinates) return;

        if (existing) {
            previousMarkers.delete(key);
            existing.venue = venue;
            existing.marker.setLatLng(coordinates);
            existing.marker.setIcon(markerIconFor(venue.status));
            existing.marker.setPopupContent(markerPopupFor(venue));
            nextMarkers.push(existing);
        } else {
            nextMarkers.push(createVenueMarker(venue, coordinates));
        }
    });

    previousMarkers.forEach(({ marker }) => venueMarkerGroup.removeLayer(marker));
    venueMarkers.splice(0, venueMarkers.length, ...nextMarkers);
    filterVenues();

    if (!hasFitVenueBounds && venueMarkerGroup.getLayers().length > 0) {
        map.fitBounds(venueMarkerGroup.getBounds(), {
            padding: [30, 30],
            maxZoom: 17
        });
        hasFitVenueBounds = true;
    }
}

// Refreshes the local read-only cache populated from the published CSV.
async function loadVenueMarkers() {
    if (refreshInProgress) return false;
    refreshInProgress = true;
    document.getElementById('loadingStatus').textContent = 'Refreshing venue data...';
    try {
        const response = await fetch('/api/venues', { cache: 'no-store' });

        if (!response.ok) {
            throw new Error('Could not load venues');
        }

        const data = await response.json();
        if (!Array.isArray(data.venues)) {
            throw new Error('Invalid venue data');
        }
        if (
            !Array.isArray(data.status_options) ||
            !Array.isArray(data.status_filter_options)
        ) {
            throw new Error('Venue status options are missing from the API response.');
        }

        venueStatuses = data.status_options;
        configureStatusFilter(data.status_filter_options);

        const syncWarning = document.getElementById('syncWarning');
        if (syncWarning) {
            syncWarning.textContent = data.sync_warning || '';
            syncWarning.hidden = !data.sync_warning;
        }

        venuesData = data.venues;
        renderVenueCards(venuesData);

        updateVenueCounts();
        reconcileVenueMarkers(venuesData);

        document.getElementById('loadingStatus').textContent = data.sync_warning
            ? 'Showing saved venue data; Google Sheets sync failed.'
            : 'Venues updated successfully.';

        document.getElementById('lastChecked').textContent =
            'Last checked: ' + new Date().toLocaleTimeString();
        return true;

    } catch (error) {
        console.error('Map or venue refresh error:', error);

        document.getElementById('loadingStatus').textContent =
            'Unable to refresh venues. Check your connection.';
        const syncWarning = document.getElementById('syncWarning');
        syncWarning.textContent =
            'Unable to reach the venue data service. Showing the last loaded data.';
        syncWarning.hidden = false;
        return false;
    } finally {
        refreshInProgress = false;
    }
}

// Filter both venue cards and map markers.
function filterVenues() {
    const searchInput = document.getElementById('search');

    const searchText = searchInput.value.toLowerCase().trim();
    const selectedStatus = statusFilter.value.toLowerCase();

    // Filter venue cards.
    const cards = document.querySelectorAll('#venueList .venue');

    cards.forEach(card => {
        const cardText = card.innerText.toLowerCase();

        const statusElement = card.querySelector('.venue-status');
        const statusText = statusElement?.textContent.toLowerCase().trim() || '';

        const matchesSearch = cardText.includes(searchText);

        const matchesStatus =
            selectedStatus === 'all' ||
            statusText === selectedStatus;

        card.style.display =
            matchesSearch && matchesStatus ? '' : 'none';
    });

    // Filter map markers using their associated venue data.
    venueMarkers.forEach(item => {
        const venue = item.venue;
        const marker = item.marker;

        const venueText = (
            (venue.venue_name || '') + ' ' +
            (venue.location || '') + ' ' +
            (venue.status || '') + ' ' +
            (venue.updated_at || '')
        ).toLowerCase();

        const venueStatus = normalizedVenueStatus(venue.status).toLowerCase();

        const matchesSearch = venueText.includes(searchText);

        const matchesStatus =
            selectedStatus === 'all' ||
            venueStatus === selectedStatus;

        if (matchesSearch && matchesStatus) {
            if (!venueMarkerGroup.hasLayer(marker)) {
                venueMarkerGroup.addLayer(marker);
            }
        } else {
            if (venueMarkerGroup.hasLayer(marker)) {
                venueMarkerGroup.removeLayer(marker);
            }
        }
    });
}

// Connect the controls to the filter function.
document.getElementById('search')
    .addEventListener('input', filterVenues);

document.getElementById('statusFilter')
    .addEventListener('change', filterVenues);

venueList.addEventListener('click', event => {
    const target = event.target;
    const card = target.closest('.venue');
    if (!card) return;

    if (target.closest('[data-edit-venue]')) {
        card.querySelector('[data-venue-edit]').hidden = false;
        return;
    }
    if (target.closest('[data-cancel-edit]')) {
        card.querySelector('[data-venue-edit]').hidden = true;
        return;
    }
    if (target.closest('[data-delete-venue]')) {
        if (!window.confirm('Delete this venue from the Google Sheet?')) return;
        sendAdminRequest(`/api/admin/venues/${card.dataset.venueId}`, 'DELETE')
            .then(async data => {
                showAdminNotice(data.message || 'Venue deleted.', false);
                if (!await loadVenueMarkers()) {
                    showAdminNotice(
                        'The Sheet was updated, but venue data could not be refreshed. Reload the page or check the connection.',
                        true
                    );
                }
            })
            .catch(error => {
                showAdminNotice(error.message || 'The venue could not be deleted.', true);
            });
        return;
    }
    if (target.closest('form, button, input, select, a')) return;

    focusVenueOnMap(card);
});

venueList.addEventListener('keydown', event => {
    const card = event.target.closest('.venue');
    if (
        card &&
        event.target === card &&
        (event.key === 'Enter' || event.key === ' ')
    ) {
        event.preventDefault();
        focusVenueOnMap(card);
    }
});

if (isAdmin) {
    document.getElementById('createVenueForm')
        .addEventListener('submit', event => {
            event.preventDefault();
            submitAdminForm(
                event.currentTarget,
                '/api/admin/venues',
                'POST',
                'Venue added to Google Sheets.'
            );
        });

    venueList.addEventListener('submit', event => {
        const form = event.target.closest('[data-venue-edit]');
        if (!form) return;
        event.preventDefault();
        const card = form.closest('.venue');
        submitAdminForm(
            form,
            `/api/admin/venues/${card.dataset.venueId}`,
            'PUT',
            'Venue updated in Google Sheets.'
        );
    });
}

if ('ResizeObserver' in window) {
    new ResizeObserver(() => map.invalidateSize()).observe(mapElement);
} else {
    window.addEventListener('resize', () => map.invalidateSize());
}
window.requestAnimationFrame(() => map.invalidateSize());

// Start loading and refresh every 30 seconds.
loadVenueMarkers();
setInterval(loadVenueMarkers, 30000);
