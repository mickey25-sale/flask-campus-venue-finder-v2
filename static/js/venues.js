// Create the map centred approximately on the Alupe area.
const map = L.map('map').setView([0.50396, 34.12548], 15);

L.tileLayer(
    'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    {
        attribution: '&copy; OpenStreetMap contributors'
    }
).addTo(map);

// Each item contains its Leaflet marker and venue data.
const venueMarkers = [];

// Safely display text supplied by the spreadsheet.
function escapeMapText(value) {
    const element = document.createElement('span');
    element.textContent = value || 'Not specified';
    return element.innerHTML;
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

const isAdmin = document.body.dataset.isAdmin === 'true';
const venueList = document.getElementById('venueList');
let venuesData = [];

function focusVenueOnMap(card) {
    document.querySelectorAll('#venueList .venue').forEach(item => {
        item.classList.remove('selected');
    });

    card.classList.add('selected');
    const selected = venueMarkers.find(item =>
        String(item.venue.id) === card.dataset.venueId
    );

    if (selected) {
        map.setView(selected.marker.getLatLng(), 18);
        selected.marker.openPopup();
    }
}

function renderVenueCards(venues) {
    if (venues.length === 0) {
        venueList.innerHTML = '<p class="empty-state">No venues yet. Add a venue to get started.</p>';
        return;
    }

    venueList.innerHTML = venues.map(venue => {
        const status = (venue.status || 'Unknown').trim();
        const statusClass = ['free', 'occupied'].includes(status.toLowerCase())
            ? status.toLowerCase()
            : 'unknown';
        const venueName = escapeMapText(venue.venue_name || 'Unnamed venue');
        const location = escapeMapText(venue.location || 'Not specified');
        const updatedAt = escapeMapText(formatUpdatedAt(venue.updated_at));
        const directions = escapeMapText(getDirectionsUrl(venue));
        const actions = isAdmin
            ? `<div class="venue-actions">
                <select class="availability-select" aria-label="Update availability" data-action="availability">
                    <option value="Free" ${statusClass === 'free' ? 'selected' : ''}>Free</option>
                    <option value="Occupied" ${statusClass === 'occupied' ? 'selected' : ''}>Occupied</option>
                    <option value="Unknown" ${statusClass === 'unknown' ? 'selected' : ''}>Unknown</option>
                </select>
                <button class="action-button" type="button" data-action="edit">Edit</button>
                <button class="action-button delete" type="button" data-action="delete">Delete</button>
            </div>`
            : '';

        return `<article class="venue" data-venue-id="${Number(venue.id)}" onclick="if (!event.target.closest('.venue-actions')) focusVenueOnMap(this)" tabindex="0" role="button" aria-label="Show venue on map">
            <h3>${venueName}</h3>
            <p class="venue-location">${location}</p>
            <a class="venue-directions" href="${directions}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">↗ Get directions</a>
            <div class="venue-meta">
                <span class="venue-status ${statusClass}">${escapeMapText(status)}</span>
                <p class="venue-updated">Updated ${updatedAt}</p>
            </div>
            ${actions}
        </article>`;
    }).join('');
}

function getDirectionsUrl(venue) {
    const latitude = Number(venue.latitude);
    const longitude = Number(venue.longitude);
    const hasCoordinates =
        String(venue.latitude || '').trim() !== '' &&
        String(venue.longitude || '').trim() !== '' &&
        Number.isFinite(latitude) &&
        Number.isFinite(longitude) &&
        Math.abs(latitude) <= 90 &&
        Math.abs(longitude) <= 180;
    const destination = hasCoordinates
        ? `${latitude},${longitude}`
        : `${venue.venue_name || ''} ${venue.location || ''}`.trim();
    const params = new URLSearchParams({
        api: '1',
        destination,
        travelmode: 'walking'
    });
    return `https://www.google.com/maps/dir/?${params.toString()}`;
}

async function sendVenueRequest(url, method, body) {
    const headers = {
        'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content
    };
    const options = { method, headers };
    if (body !== undefined) {
        headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(body);
    }

    const response = await fetch(url, options);
    const result = await response.json();
    if (!response.ok) {
        throw new Error(result.error || 'The venue change could not be saved.');
    }
    return result;
}

function showAdminMessage(message, isError = false) {
    const messageElement = document.getElementById('adminMessage');
    if (!messageElement) return;
    messageElement.textContent = message;
    messageElement.classList.toggle('error', isError);
}

const venueDialog = document.getElementById('venueDialog');
const venueForm = document.getElementById('venueForm');
let editingVenueId = null;

function openVenueDialog(venue = null) {
    editingVenueId = venue ? venue.id : null;
    venueForm.reset();
    venueForm.elements.status.value = venue?.status || 'Free';
    venueForm.elements.venue_name.value = venue?.venue_name || '';
    venueForm.elements.location.value = venue?.location || '';
    venueForm.elements.latitude.value = venue?.latitude || '';
    venueForm.elements.longitude.value = venue?.longitude || '';
    document.getElementById('venueDialogTitle').textContent =
        venue ? 'Edit venue' : 'Add a venue';
    document.getElementById('saveVenueButton').textContent =
        venue ? 'Save changes' : 'Save venue';
    venueDialog.showModal();
    venueForm.elements.venue_name.focus();
}

// Create or refresh the markers from Flask.
async function loadVenueMarkers() {
    try {
        const response = await fetch('/api/venues');

        if (!response.ok) {
            throw new Error('Could not load venues');
        }

        const data = await response.json();
        if (!Array.isArray(data.venues)) {
            throw new Error('Invalid venue data');
        }

        venuesData = data.venues;
        renderVenueCards(venuesData);

        const venueCounts = data.venues.reduce((counts, venue) => {
            const status = (venue.status || 'Unknown').trim().toLowerCase();
            counts.total += 1;
            if (status === 'free') counts.free += 1;
            if (status === 'occupied') counts.occupied += 1;
            return counts;
        }, { total: 0, free: 0, occupied: 0 });

        document.getElementById('totalCount').textContent = venueCounts.total;
        document.getElementById('freeCount').textContent = venueCounts.free;
        document.getElementById('occupiedCount').textContent = venueCounts.occupied;

        // Remove existing markers.
        venueMarkers.forEach(item => {
            map.removeLayer(item.marker);
        });
        venueMarkers.length = 0;

        // Add a marker for each venue with valid coordinates.
        data.venues.forEach(venue => {
            const latValue = venue.latitude;
            const lonValue = venue.longitude;


            if (
                latValue == null ||
                lonValue == null ||
                String(latValue).trim() === '' ||
                String(lonValue).trim() === ''
            ) {
                return;
            }

            const latitude = Number(latValue);
            const longitude = Number(lonValue);

            if (
                !Number.isFinite(latitude) ||
                !Number.isFinite(longitude) ||
                Math.abs(latitude) > 90 ||
                Math.abs(longitude) > 180
            ) {
                return;
            }

            const status = (venue.status || 'Unknown').trim();

            const markerColor =
                status.toLowerCase() === 'free' ? 'green' :
                status.toLowerCase() === 'occupied' ? 'red' :
                'orange';

            const marker = L.circleMarker(
                [latitude, longitude],
                {
                    radius: 9,
                    color: markerColor,
                    fillColor: markerColor,
                    fillOpacity: 0.8
                }
            );
            marker.bindPopup(
                '<b>' + escapeMapText(venue.venue_name) + '</b><br>' +
                'Location: ' + escapeMapText(venue.location) + '<br>' +
                'Status: ' + escapeMapText(status) + '<br>' +
                '<a href="' + escapeMapText(getDirectionsUrl(venue)) +
                '" target="_blank" rel="noopener noreferrer">Get directions</a>'
            );
            marker.addTo(map);
            venueMarkers.push({
                marker: marker,
                venue: venue
            });
        });
        // Apply current filters to the refreshed markers.
        filterVenues();

        // Fit the map around all markers, if available.
        if (venueMarkers.length > 0) {
            const markergroup = L.featureGroup(
                venueMarkers.map(item => item.marker)
            );

            map.fitBounds(markergroup.getBounds(), {
                padding:[30,30],
                maxZoom: 17
        });
        }

        document.getElementById('loadingStatus').textContent =
            'Venues updated successfully.';

        document.getElementById('lastChecked').textContent =
            'Last checked: ' + new Date().toLocaleTimeString();

    } catch (error) {
        console.error('Map or venue refresh error:', error);

        document.getElementById('loadingStatus').textContent =
            'Unable to refresh venues. Check your connection.';
        showAdminMessage(error.message, true);
    }
}

// Filter both venue cards and map markers.
function filterVenues() {
    const searchInput = document.getElementById('search');
    const statusInput = document.getElementById('statusFilter');

    const searchText = searchInput.value.toLowerCase().trim();
    const selectedStatus = statusInput.value.toLowerCase();

    // Filter venue cards.
    const cards = document.querySelectorAll('#venueList .venue');

    cards.forEach(card => {
        const cardText = card.innerText.toLowerCase();

        const statusElement = card.querySelector('.venue-status');
        const statusText = statusElement
            ? statusElement.textContent.toLowerCase().trim()
            : 'unknown';

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

        const venueStatus =
            (venue.status || 'Unknown').toLowerCase().trim();

        const matchesSearch = venueText.includes(searchText);

        const matchesStatus =
            selectedStatus === 'all' ||
            venueStatus === selectedStatus;

        if (matchesSearch && matchesStatus) {
            if (!map.hasLayer(marker)) {
                marker.addTo(map);
            }
        } else {
            if (map.hasLayer(marker)) {
                map.removeLayer(marker);
            }
        }
    });
}

// Connect the controls to the filter function.
document.getElementById('search')
    .addEventListener('input', filterVenues);

document.getElementById('statusFilter')
    .addEventListener('change', filterVenues);

venueList.addEventListener('keydown', event => {
    const card = event.target.closest('.venue');
    if (card && event.target === card && (event.key === 'Enter' || event.key === ' ')) {
        event.preventDefault();
        focusVenueOnMap(card);
    }
});

if (isAdmin) {
    document.getElementById('addVenueButton').addEventListener('click', () => {
        openVenueDialog();
    });

    document.getElementById('closeVenueDialog').addEventListener('click', () => {
        venueDialog.close();
    });
    document.getElementById('cancelVenueDialog').addEventListener('click', () => {
        venueDialog.close();
    });

    venueForm.addEventListener('submit', async event => {
        event.preventDefault();
        const formValues = Object.fromEntries(new FormData(venueForm).entries());
        const saveButton = document.getElementById('saveVenueButton');
        saveButton.disabled = true;
        showAdminMessage('');

        try {
            await sendVenueRequest(
                editingVenueId
                    ? `/api/venues/${editingVenueId}`
                    : '/api/venues',
                editingVenueId ? 'PUT' : 'POST',
                formValues
            );
            venueDialog.close();
            showAdminMessage(editingVenueId ? 'Venue updated.' : 'Venue added.');
            await loadVenueMarkers();
        } catch (error) {
            showAdminMessage(error.message, true);
        } finally {
            saveButton.disabled = false;
        }
    });

    venueList.addEventListener('click', async event => {
        const button = event.target.closest('button[data-action]');
        if (!button) return;

        const card = button.closest('.venue');
        const venue = venuesData.find(item =>
            String(item.id) === card.dataset.venueId
        );
        if (!venue) return;

        if (button.dataset.action === 'edit') {
            openVenueDialog(venue);
            return;
        }

        if (button.dataset.action === 'delete') {
            if (!window.confirm(`Delete "${venue.venue_name}"? This cannot be undone.`)) {
                return;
            }
            try {
                await sendVenueRequest(`/api/venues/${venue.id}`, 'DELETE');
                showAdminMessage('Venue deleted.');
                await loadVenueMarkers();
            } catch (error) {
                showAdminMessage(error.message, true);
            }
        }
    });

    venueList.addEventListener('change', async event => {
        const select = event.target.closest('select[data-action="availability"]');
        if (!select) return;

        const card = select.closest('.venue');
        const venueId = card.dataset.venueId;
        try {
            await sendVenueRequest(
                `/api/venues/${venueId}/availability`,
                'PATCH',
                { status: select.value }
            );
            showAdminMessage('Availability updated.');
            await loadVenueMarkers();
        } catch (error) {
            showAdminMessage(error.message, true);
            await loadVenueMarkers();
        }
    });
}

// Start loading and refresh every 30 seconds.
loadVenueMarkers();
setInterval(loadVenueMarkers, 30000);
