"""Internal endpoints for deployment automation.
Not exposed publicly — only accessible from localhost via deploy scripts."""
import os
from quart import Blueprint, request, jsonify

internal_bp = Blueprint('internal', __name__, url_prefix='/internal')

INTERNAL_TOKEN = os.environ.get('INTERNAL_API_TOKEN', '')


def _check_token():
    """Verify request comes with valid internal token (or allow when unset for self-host)."""
    if not INTERNAL_TOKEN:
        return True
    token = request.headers.get('X-Internal-Token', '')
    return token == INTERNAL_TOKEN


@internal_bp.route('/reload-anime', methods=['POST'])
async def reload_anime():
    if not _check_token():
        return jsonify({'error': 'unauthorized'}), 403

    from ..lib.anime_mapping import update_database
    try:
        updated = update_database()
        return jsonify({'updated': updated}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@internal_bp.route('/clear-cache', methods=['POST'])
async def clear_cache():
    """Clear provider search disk cache and in-memory app caches."""
    if not _check_token():
        return jsonify({'error': 'unauthorized'}), 403

    from ..lib.provider_search_cache import clear_provider_search_cache
    from ..extensions import cache

    try:
        removed_files = clear_provider_search_cache()
        cache.clear()
        return jsonify({
            'provider_search_files_removed': removed_files,
            'in_memory_cache_cleared': True,
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500
