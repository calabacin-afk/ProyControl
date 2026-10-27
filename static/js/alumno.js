// Este archivo conecta el buscador con las tarjetas de clase del panel del alumno.
var searchInput = document.getElementById('class-search');
var classCards = document.querySelectorAll('.class-card');
var noResults = document.getElementById('no-results');

if (searchInput) {
    searchInput.addEventListener('input', function () {
        // Cada vez que se escribe, dejamos visibles solo las clases que coinciden.
        var query = searchInput.value.trim().toLocaleLowerCase('es');
        var visibleCount = 0;

        for (var index = 0; index < classCards.length; index += 1) {
            var card = classCards[index];
            var matches = card.dataset.search.indexOf(query) !== -1;
            card.hidden = !matches;
            if (matches) {
                visibleCount += 1;
            }
        }

        noResults.hidden = visibleCount > 0;
    });
}
