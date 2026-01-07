// Bar Chart
new Chart(document.getElementById('barChart'), {
    type: 'bar',
    data: {
        labels: ['Jan','Feb','Mar','Apr','May','Jun'],
        datasets: [
            {
                label: 'Orders',
                data: [12,19,8,15,22,14],
            },
            {
                label: 'Revenue',
                data: [20,14,18,25,30,22],
            }
        ]
    }
});

// Donut Chart
new Chart(document.getElementById('donutChart'), {
    type: 'doughnut',
    data: {
        datasets: [{
            data: [45, 55],
        }]
    },
    options: {
        cutout: '70%',
        plugins: { legend: { display: false } }
    }
});
