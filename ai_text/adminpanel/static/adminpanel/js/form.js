document.querySelectorAll(".toggle-eye").forEach(icon => {
    icon.addEventListener("click", function () {
        const inputId = this.getAttribute("data-target");
        const input = document.getElementById(inputId);

        if (input.type === "password") {
            input.type = "text";
            this.classList.remove("fa-eye");
            this.classList.add("fa-eye-slash");
        } else {
            input.type = "password";
            this.classList.remove("fa-eye-slash");
            this.classList.add("fa-eye");
        }
    });
});


document.getElementById("createUserForm").addEventListener("submit", function (e) {
    const password = document.getElementById("password").value;
    const confirm = document.getElementById("confirm_password").value;

    let valid = true;

    // reset messages
    document.getElementById("passwordHelp").textContent = "";
    document.getElementById("confirmHelp").textContent = "";

    // strong password rules
    const strongRegex =
        /^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&]).{8,}$/;

    if (!strongRegex.test(password)) {
        document.getElementById("passwordHelp").textContent =
            "Password must be 8+ chars, include uppercase, lowercase, number & symbol.";
        valid = false;
    }

    if (password !== confirm) {
        document.getElementById("confirmHelp").textContent =
            "Passwords do not match.";
        valid = false;
    }

    if (!valid) e.preventDefault();
});
